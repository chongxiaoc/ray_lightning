from typing import Callable, Dict, List, Union, Any

from collections import defaultdict
from contextlib import closing
import os
import socket

import numpy as np
import torch

import pytorch_lightning as pl
from pytorch_lightning.plugins import DDPSpawnPlugin
from pytorch_lightning import _logger as log, LightningModule
from pytorch_lightning.trainer.states import TrainerFn
from pytorch_lightning.utilities import rank_zero_only, rank_zero_info
from pytorch_lightning.utilities.apply_func import apply_to_collection
from pytorch_lightning.utilities.seed import reset_seed

import ray
from ray.util import PublicAPI
from ray.util.queue import Queue

from ray_lightning.session import init_session
from ray_lightning.util import process_results, to_state_stream, \
    load_state_stream
from ray_lightning.tune import TUNE_INSTALLED, is_session_enabled


def find_free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@ray.remote
class RayExecutor:
    """A class to execute any arbitrary function remotely."""

    def set_env_var(self, key: str, value: str):
        """Set an environment variable with the provided values."""
        if value is not None:
            value = str(value)
            os.environ[key] = value

    def set_env_vars(self, keys: List[str], values: List[str]):
        """Sets multiple env vars with the provided values"""
        assert len(keys) == len(values)
        for key, value in zip(keys, values):
            self.set_env_var(key, value)

    def get_node_ip(self):
        """Returns the IP address of the node that this Ray actor is on."""
        return ray.util.get_node_ip_address()

    def execute(self, fn: Callable, *args, **kwargs):
        """Execute the provided function and return the result."""
        return fn(*args, **kwargs)


@PublicAPI(stability="beta")
class RayPlugin(DDPSpawnPlugin):
    """Pytorch Lightning plugin for DDP training on a Ray cluster.

    This plugin is used to manage distributed training using DDP and
    Ray for process launching. Internally, the specified number of
    Ray actors are launched in the cluster and are registered as part of a
    Pytorch DDP process group. The Pytorch Lightning trainer is instantiated
    on the driver and sent to each of these training workers where training is
    executed. The distributed training protocol is handled by Pytorch DDP.

    Each training worker is configured to reserve ``num_cpus_per_worker``
    CPUS and 1 GPU if ``use_gpu`` is set to ``True``.

    If using this plugin, you should run your code like a normal Python
    script: ``python train.py``, and only on the head node if running in a
    distributed Ray cluster. There is no need to run this script on every
    single node.

    Args:
        num_workers (int): Number of training workers to use.
        num_cpus_per_worker (int): Number of CPUs per worker.
        use_gpu (bool): Whether to use GPU for allocation. For GPU to be
            used, you must also set the ``gpus`` arg in your Pytorch Lightning
            Trainer to a value > 0.
        init_hook (Callable): A function to run on each worker
            upon instantiation.
        **ddp_kwargs: Additional arguments to pass into
            ``DistributedDataParallel`` initialization

    Example:

        .. code-block:: python

            import pytorch_lightning as ptl
            from ray_lightning import RayAccelerator

            ptl_model = MNISTClassifier(...)
            plugin = RayPlugin(num_workers=4, cpus_per_worker=1,
                use_gpu=True)

            # If using GPUs, set the ``gpus`` arg to a value > 0.
            # The actual number of GPUs is determined by ``num_workers``.
            trainer = pl.Trainer(..., gpus=1, plugins=[plugin])
            trainer.fit(ptl_model)

    """

    def __init__(self,
                 num_workers: int = 1,
                 num_cpus_per_worker: int = 1,
                 use_gpu: bool = False,
                 init_hook: Callable = None,
                 **ddp_kwargs: Union[Any, Dict[str, Any]]):
        if not ray.is_initialized():
            ray.init()
        self._is_remote = False
        self.nickname = "ddp_ray"
        self.num_workers = num_workers
        self.num_cpus_per_worker = num_cpus_per_worker
        self.use_gpu = use_gpu
        self.workers = []
        self.init_hook = init_hook

        self._local_rank = 0
        self._global_rank = 0
        self._node_rank = 0

        super().__init__(
            parallel_devices=[], cluster_environment=None, **ddp_kwargs)

    def __getstate__(self):
        d = self.__dict__.copy()
        # Don't serialize the workers.
        del d["workers"]
        return d

    def __setstate__(self, d):
        d["workers"] = []
        self.__dict__.update(d)

    def _create_worker(self):
        """Creates Ray actor."""
        worker = RayExecutor.options(
            num_cpus=self.num_cpus_per_worker,
            num_gpus=int(self.use_gpu)).remote()
        return worker

    def setup(self):
        """Sets up PTL Trainer and creates the Ray actors."""
        self.workers = [self._create_worker() for _ in range(self.num_workers)]
        if self.init_hook:
            ray.get([w.execute.remote(self.init_hook) for w in self.workers])

    def _setup_env_vars(self):
        # Get rank 0 worker address and port for DDP connection.
        os.environ["MASTER_ADDR"] = ray.get(
            self.workers[0].get_node_ip.remote())
        os.environ["MASTER_PORT"] = str(
            ray.get(self.workers[0].execute.remote(find_free_port)))

        # Set environment variables for remote workers.
        keys = [
            "PL_GLOBAL_SEED", "PL_TORCH_DISTRIBUTED_BACKEND", "MASTER_ADDR",
            "MASTER_PORT"
        ]
        values = [os.getenv(k) for k in keys]
        ray.get([w.set_env_vars.remote(keys, values) for w in self.workers])

    def start_training(self, trainer):
        results = self.execution_loop(tune_enabled=True)
        # reset optimizers, since main process is never used for training and
        # thus does not have a valid optim state.
        trainer.optimizers = []
        return results

    def start_evaluating(self, trainer):
        results = self.execution_loop(tune_enabled=False)
        return results

    def start_predicting(self, trainer):
        results = self.execution_loop(tune_enabled=False)
        return results

    def get_local_ranks(self) -> Dict[int, int]:
        """Creates a mapping of global ranks to local ranks/node ranks."""
        # Get the local ranks for all the workers and store as a dict.
        # First get the IP address of each remote worker.
        node_ips = ray.get([w.get_node_ip.remote() for w in self.workers])

        node_ip_map = {}
        for i in range(len(node_ips)):
            node_ip_map[node_ips[i]] = i

        rank_counter_dict = defaultdict(int)
        global_to_local = [None] * self.num_workers

        for global_rank in range(self.num_workers):
            ip = node_ips[global_rank]
            global_to_local[global_rank] = (rank_counter_dict[ip],
                                            node_ip_map[ip])
            rank_counter_dict[ip] += 1

        return global_to_local

    def execution_loop(self, tune_enabled: bool = True):
        """Main execution loop for training, testing, & prediction.

        Sets up the torch.distributed process group for each
        worker. Then trigger remote training/testing/eval via
        ``train_remote`` on each worker. If using with Ray Tune, create a
        communication queue to retrieve intermediate results, and process
        those results. Finally retrieve the training results from the rank 0
        worker and return.
        """

        # Sets environment variables for all workers.
        # This will set the MASTER_ADDR and MASTER_PORT on each Ray actor.
        self._setup_env_vars()

        # Get the mapping from global ranks to the respective local ranks.
        self.global_to_local = self.get_local_ranks()

        model = self._model
        model_ref = ray.put(model)
        # Don't pickle the model when training remotely.
        self._model = None

        queue = None
        if tune_enabled and TUNE_INSTALLED and is_session_enabled():
            # Create communication queue and send to all the workers.
            queue = Queue(actor_options={"num_cpus": 0})

        futures = [
            self.workers[i].execute.remote(self.execute_remote, model_ref, i,
                                           queue)
            for i in range(self.num_workers)
        ]

        results = process_results(futures, queue)

        # DDPSpawn.__recover_child_process_weights begin
        # Get the results, checkpoint path, and model weights from worker 0.
        results, best_path, state_stream, callback_metrics = results[0]
        state_dict = load_state_stream(state_stream, to_gpu=self.use_gpu)
        # Set the state for PTL using the output from remote training.
        self._results = results
        self._model = model
        self._model.load_state_dict(state_dict)
        if self.lightning_module.trainer.checkpoint_callback:
            self.lightning_module.trainer.checkpoint_callback \
                .best_model_path = best_path

        # From DDPSpawn.get_queue
        self.lightning_module.trainer.callback_metrics.update(
            apply_to_collection(callback_metrics,
                                np.ndarray, lambda x: torch.tensor(x)))

        # DDPSpawn.__recover_child_process_weights_end

        if queue:
            # Shutdown the queue.
            queue.shutdown()

    def post_dispatch(self, trainer: "pl.Trainer"):
        """Shutdown the DDP process group and all the Ray actors."""

        def shutdown_remote():
            torch.distributed.destroy_process_group()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        ray.get([w.execute.remote(shutdown_remote) for w in self.workers])
        for w in self.workers:
            ray.kill(w, no_restart=True)
            del w
        self.workers = []

    # All methods below are only executed in remote Ray workers.

    def set_world_ranks(self, process_idx: int = 0):
        """Set the appropriate rank attributes for the trainer."""
        if self._is_remote:
            self._global_rank = process_idx
            self._local_rank, self._node_rank = self.global_to_local[
                self.global_rank]

    def _worker_setup(self, process_idx: int):
        reset_seed()
        self.set_world_ranks(process_idx)
        rank_zero_only.rank = self.global_rank

        # Taken from pytorch_lightning.utilities.distributed
        # .init_dist_connection
        # Modified to not use cluster environment.
        if torch.distributed.is_available(
        ) and not torch.distributed.is_initialized():
            log.info(
                f"initializing distributed: GLOBAL_RANK: {self.global_rank}, "
                f"MEMBER: {self.global_rank + 1}/{self.world_size}")
            torch.distributed.init_process_group(
                self.torch_distributed_backend,
                rank=self.global_rank,
                world_size=self.world_size)

            # on rank=0 let everyone know training is starting
            rank_zero_info(
                f"{'-' * 100}\n"
                f"distributed_backend={self.torch_distributed_backend}\n"
                f"All distributed processes registered. Starting with "
                f"{self.world_size} processes\n"
                f"{'-' * 100}\n")

    def execute_remote(self,
                       model: LightningModule,
                       global_rank: int,
                       queue: Queue = None):
        """Train/test/eval function to be executed on each remote worker.

        Modified from DDPSpawn._wrapped_function and DDPSpawn.new_process

        """
        assert isinstance(self, RayPlugin)
        # This method should be executed remotely in each worker.
        self._model = model
        self.lightning_module.trainer._accelerator_connector \
            ._training_type_plugin = self
        self.lightning_module.trainer._accelerator_connector.accelerator \
            .training_type_plugin = self

        # TODO: See if this is necessary.
        self.lightning_module.trainer._data_connector.prepare_data()

        # Set _is_remote to True so that self.set_world_ranks will
        # properly set the ranks.
        self._is_remote = True

        if queue is not None:
            # Initialize session.
            init_session(rank=global_rank, queue=queue)

        self._worker_setup(process_idx=global_rank)

        # Below is modified from DDPSpawn.new_process

        # Move the model to the correct device.
        self.model_to_device()

        # TODO: Support syncbatchnorm.
        # skip wrapping the model if we are not fitting as no gradients
        # need to be exchanged.
        trainer_fn = self.lightning_module.trainer.state.fn
        if trainer_fn == TrainerFn.FITTING:
            self.configure_ddp()

        self.barrier()

        results = self.lightning_module.trainer.run_stage()

        # __transfer_distrib_spawn_state_on_fit_end start
        if self.global_rank == 0:
            checkpoint_callback = \
                self.lightning_module.trainer.checkpoint_callback
            best_model_path = checkpoint_callback.best_model_path if \
                checkpoint_callback else None

            # PyTorch Lightning saves the model weights in a temp file and
            # loads it back on the driver.
            # This won't work in a multi-node setup though, so we return the
            # model state stream directly.
            model_state_stream = to_state_stream(
                self.lightning_module.state_dict())

            # From DDPSpawn.add_to_queue
            callback_metrics: dict = apply_to_collection(
                self.lightning_module.trainer.callback_metrics,
                torch.Tensor, lambda x: x.cpu().numpy(
                ))  # send as numpy to avoid issues with memory sharing

            return_val = results, best_model_path, model_state_stream, \
                callback_metrics
        else:
            return_val = None
        # __transfer_distrib_spawn_state_on_fit_end end

        self.lightning_module.trainer._call_teardown_hook()

        return return_val

    @property
    def world_size(self) -> int:
        return self.num_workers

    @property
    def local_rank(self) -> int:
        return self._local_rank

    @property
    def global_rank(self) -> int:
        return self._global_rank

    @property
    def node_rank(self) -> int:
        return self._node_rank

    @property
    def root_device(self):
        if self.use_gpu and torch.cuda.is_available():
            # Ray already sets CUDA_VISIBLE_DEVICES for each process.
            # So the device is the 0th index in CUDA_VISIBLE_DEVICES
            return torch.device("cuda", 0)
        else:
            return torch.device("cpu")

    @property
    def distributed_sampler_kwargs(self):
        """Returns the args to use for torch.data.DistributedSampler."""
        distributed_sampler_kwargs = dict(
            num_replicas=self.num_workers, rank=self.global_rank)
        return distributed_sampler_kwargs

    @property
    def _is_single_process_single_device(self):
        return True
