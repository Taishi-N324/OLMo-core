import logging
import sys
from dataclasses import dataclass
from typing import Callable, List, cast

from rich import print

from olmo_core.config import Config, StrEnum
from olmo_core.data import NumpyDataLoaderConfig, NumpyDatasetConfig
from olmo_core.distributed.utils import get_local_rank
from olmo_core.launch.beaker import BeakerLaunchConfig
from olmo_core.model_ladder import ModelLadder, ModelSize, RunDuration
from olmo_core.nn.transformer import TransformerConfig
from olmo_core.train import (
    TrainerConfig,
    prepare_training_environment,
    teardown_training_environment,
)
from olmo_core.train.callbacks import CometCallback, ConfigSaverCallback, WandBCallback
from olmo_core.train.train_module import TransformerTrainModuleConfig
from olmo_core.utils import prepare_cli_environment, seed_all

from .common import build_launch_config, get_gpu_type, get_root_dir

log = logging.getLogger(__name__)


@dataclass
class LadderRunConfig(Config):
    launch: BeakerLaunchConfig
    ladder: ModelLadder
    model: TransformerConfig
    dataset: NumpyDatasetConfig
    data_loader: NumpyDataLoaderConfig
    train_module: TransformerTrainModuleConfig
    trainer: TrainerConfig


class SubCmd(StrEnum):
    launch = "launch"
    train = "train"
    dry_run = "dry_run"

    def prepare_environment(self):
        if self in (SubCmd.launch, SubCmd.dry_run):
            prepare_cli_environment()
        elif self == SubCmd.train:
            prepare_training_environment()
        else:
            raise NotImplementedError(self)

    def run(self, config: LadderRunConfig):
        if get_local_rank() == 0:
            print(config)

        if self == SubCmd.launch:
            config.launch.launch(follow=True)
        elif self == SubCmd.dry_run:
            pass
        elif self == SubCmd.train:
            try:
                # Set RNG states on all devices.
                seed_all(config.ladder.init_seed)

                # Build components.
                model = config.model.build(init_device="meta")
                train_module = config.train_module.build(model)
                dataset = config.dataset.build()
                data_loader = config.data_loader.build(
                    dataset, dp_process_group=train_module.dp_process_group
                )
                trainer = config.trainer.build(train_module, data_loader)

                # Record the config to W&B/Comet and each checkpoint dir.
                config_dict = config.as_config_dict()
                cast(CometCallback, trainer.callbacks["comet"]).config = config_dict
                cast(WandBCallback, trainer.callbacks["wandb"]).config = config_dict
                cast(ConfigSaverCallback, trainer.callbacks["config_saver"]).config = config_dict

                # Train.
                trainer.fit()
            finally:
                teardown_training_environment()
        else:
            raise NotImplementedError(self)


def build_config(
    ladder: ModelLadder,
    script: str,
    size: ModelSize,
    run_duration: RunDuration,
    cmd: SubCmd,
    cluster: str,
    overrides: List[str],
) -> LadderRunConfig:
    del cmd

    root_dir = get_root_dir(cluster)
    launch = build_launch_config(
        name=f"{ladder.name}-{size}-{run_duration}",
        root_dir=root_dir,
        cmd=[script, SubCmd.train, size, run_duration, cluster, *overrides],
        cluster=cluster,
    ).merge(overrides, strict=False)

    dp_world_size = launch.num_nodes * launch.num_gpus
    gpu_type = get_gpu_type(cluster)

    model = ladder.get_model_config(size=size)
    dataset = ladder.get_dataset_config()
    data_loader = ladder.get_data_loader_config(size=size)
    train_module = ladder.get_train_module_config(
        size=size, gpu_type=gpu_type, dp_world_size=dp_world_size
    )
    trainer = ladder.get_trainer_config(size=size, gpu_type=gpu_type, dp_world_size=dp_world_size)

    return LadderRunConfig(
        launch=launch,
        ladder=ladder,
        model=model,
        dataset=dataset,
        data_loader=data_loader,
        train_module=train_module,
        trainer=trainer,
    ).merge(overrides)


def main(ladder_builder: Callable[[str], ModelLadder]):
    usage = f"""
[yellow]Usage:[/] [i blue]python[/] [i cyan]{sys.argv[0]}[/] [i b magenta]{'|'.join(SubCmd)}[/] [i b]SIZE RUN_DURATION CLUSTER[/] [i][OVERRIDES...][/]

[b]Subcommands[/]
[b magenta]launch:[/]      Launch the script on Beaker with the [b magenta]train[/] subcommand.
[b magenta]train:[/]       Run the trainer. You usually shouldn't invoke the script with this subcommand directly.
             Instead use [b magenta]launch[/] or run it with torchrun.
[b magenta]dry_run:[/]     Pretty print the config to run and exit.

[b]Examples[/]
$ [i]python {sys.argv[0]} {SubCmd.launch} 1B 1xC ai2/pluto-cirrascale --launch.num_nodes=2[/]
    """.strip()

    try:
        script, cmd, size, run_duration, cluster, overrides = (
            sys.argv[0],
            SubCmd(sys.argv[1]),
            ModelSize(sys.argv[2]),
            RunDuration(sys.argv[3]),
            sys.argv[4],
            sys.argv[5:],
        )
    except (IndexError, ValueError):
        import rich

        rich.get_console().print(usage, highlight=False)
        sys.exit(1)

    cmd.prepare_environment()

    # Build ladder config.
    ladder = ladder_builder(get_root_dir(cluster))
    ladder = ladder.merge(overrides, prefix="ladder")

    # Build run config.
    config = build_config(ladder, script, size, run_duration, cmd, cluster, overrides)
    config.ladder.validate()

    # Run the cmd.
    cmd.run(config)
