import gym
import numpy as np

from typing import (
    Dict,
    List,
    Tuple,
    Type,
    Union,
    Optional,
    Any,
)

from gym.spaces import Discrete, Box

from ray.rllib.algorithms import AlgorithmConfig
from ray.rllib.algorithms.dt.dt_torch_model import DTTorchModel
from ray.rllib.evaluation.postprocessing import discount_cumsum
from ray.rllib.models.catalog import ModelCatalog
from ray.rllib.models.modelv2 import ModelV2
from ray.rllib.models.torch.mingpt import configure_gpt_optimizer
from ray.rllib.models.torch.torch_action_dist import (
    TorchDistributionWrapper,
    TorchCategorical,
    TorchDeterministic,
)
from ray.rllib.models.torch.torch_modelv2 import TorchModelV2
from ray.rllib.policy.torch_policy_v2 import TorchPolicyV2
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.utils.framework import try_import_torch
from ray.rllib.utils.annotations import override
from ray.rllib.utils.typing import (
    TrainerConfigDict,
    TensorType,
)

torch, nn = try_import_torch()
F = nn.functional


class DTTorchPolicy(TorchPolicyV2):
    def __init__(
        self,
        observation_space: gym.spaces.Space,
        action_space: gym.spaces.Space,
        config: TrainerConfigDict,
    ):
        TorchPolicyV2.__init__(
            self,
            observation_space,
            action_space,
            config,
            max_seq_len=config["model"]["max_seq_len"],
        )

    @override(TorchPolicyV2)
    def make_model_and_action_dist(
        self,
    ) -> Tuple[ModelV2, Type[TorchDistributionWrapper]]:
        # Model
        model_config = self.config["model"]
        model_config.update(
            dict(
                embed_dim=self.config["embed_dim"],
                max_seq_len=self.config["max_seq_len"],
                max_ep_len=self.config["max_ep_len"],
                num_layers=self.config["num_layers"],
                num_heads=self.config["num_heads"],
                embed_pdrop=self.config["embed_pdrop"],
                resid_pdrop=self.config["resid_pdrop"],
                attn_pdrop=self.config["attn_pdrop"],
                use_obs_output=self.config["use_obs_output"],
                use_return_output=self.config["use_return_output"],
                target_return=self.config["target_return"],
            )
        )

        num_outputs = int(np.product(self.observation_space.shape))

        model = ModelCatalog.get_model_v2(
            obs_space=self.observation_space,
            action_space=self.action_space,
            num_outputs=num_outputs,
            model_config=model_config,
            framework=self.config["framework"],
            model_interface=None,
            default_model=DTTorchModel,
            name="model",
        )

        # Action Distribution
        if isinstance(self.action_space, Discrete):
            action_dist = TorchCategorical
        elif isinstance(self.action_space, Box):
            action_dist = TorchDeterministic
        else:
            raise NotImplementedError

        return model, action_dist

    @override(TorchPolicyV2)
    def optimizer(
        self,
    ) -> Union[List["torch.optim.Optimizer"], "torch.optim.Optimizer"]:
        optimizer = configure_gpt_optimizer(
            model=self.model,
            learning_rate=self.config["lr"],
            weight_decay=self.config["weight_decay"],
            betas=self.config["betas"],
        )

        return optimizer

    @override(TorchPolicyV2)
    def postprocess_trajectory(
        self,
        sample_batch: SampleBatch,
        other_agent_batches: Optional[Dict[Any, SampleBatch]] = None,
        episode: Optional["Episode"] = None,
    ) -> SampleBatch:
        # TODO(charlesjsun): check this is only ran with one episode?
        # TODO(charlesjsun): custom discount factor
        assert len(sample_batch.split_by_episode()) == 1

        rewards = sample_batch[SampleBatch.REWARDS].reshape(-1)
        sample_batch[SampleBatch.RETURNS_TO_GO] = discount_cumsum(rewards, 1.0)

        return sample_batch

    @override(TorchPolicyV2)
    def action_distribution_fn(
        self,
        model: ModelV2,
        *,
        obs_batch: SampleBatch,
        state_batches: TensorType,
        **kwargs,
    ) -> Tuple[TensorType, type, List[TensorType]]:

        # Note: this doesn't create a new SampleBatch, so changes to obs_batch persists
        obs_batch = self._lazy_tensor_dict(obs_batch)

        batch_size = obs_batch[SampleBatch.OBS].shape[0]

        # Add current timestep (+1 because -1 is first observation)
        timesteps = obs_batch[SampleBatch.T]
        new_timestep = timesteps[:, -1:] + 1
        obs_batch[SampleBatch.T] = torch.cat([timesteps, new_timestep], dim=1)

        # mask out any padded value at start of rollout
        obs_batch[SampleBatch.ATTENTION_MASKS] = torch.where(obs_batch[SampleBatch.T] >= 0, 1.0, 0.0)

        # remove out of bound -1 timesteps after attention mask is calculated
        obs_batch[SampleBatch.T] = torch.where(obs_batch[SampleBatch.T] < 0, 0, obs_batch[SampleBatch.T])

        # compute returns to go
        rtg = obs_batch[SampleBatch.RETURNS_TO_GO]
        last_rtg = rtg[:, -1]
        last_reward = obs_batch[SampleBatch.REWARDS]
        updated_rtg = last_rtg - last_reward

        initial_rtg = torch.full((batch_size, 1), fill_value=self.config["target_return"], dtype=rtg.dtype, device=rtg.device)

        new_rtg = torch.where(new_timestep == 0, initial_rtg, updated_rtg[:, None])
        obs_batch[SampleBatch.RETURNS_TO_GO] = torch.cat([rtg, new_rtg], dim=1)[..., None]

        # Pad current action (is not actually attended to and used during inference)
        actions = obs_batch[SampleBatch.ACTIONS]
        action_pad = torch.zeros((batch_size, 1, *actions.shape[2:]), dtype=actions.dtype, device=actions.device)
        obs_batch[SampleBatch.ACTIONS] = torch.cat([actions, action_pad], dim=1)

        # Run inference on model
        model_out, _ = model(obs_batch)
        preds = self.model.get_prediction(model_out, obs_batch)
        pred_action = preds[SampleBatch.ACTIONS][:, -1]

        return pred_action, self.dist_class, []

    @override(TorchPolicyV2)
    def extra_action_out(
        self,
        input_dict: Dict[str, TensorType],
        state_batches: List[TensorType],
        model: TorchModelV2,
        action_dist: TorchDistributionWrapper,
    ) -> Dict[str, TensorType]:
        # Note: input_dict contains the updated values from action_distribution_fn
        return {
            # rtg still has the leftover extra 3rd dimension for inference
            SampleBatch.RETURNS_TO_GO: input_dict[SampleBatch.RETURNS_TO_GO][:, -1, 0],
        }

    @override(TorchPolicyV2)
    def loss(
        self,
        model: ModelV2,
        dist_class: Type[TorchDistributionWrapper],
        train_batch: SampleBatch,
    ) -> Union[TensorType, List[TensorType]]:

        train_batch = self._lazy_tensor_dict(train_batch)
        model_out, _ = self.model(train_batch)
        preds = self.model.get_prediction(model_out, train_batch)
        targets = self.model.get_targets(model_out, train_batch)
        masks = train_batch[SampleBatch.ATTENTION_MASKS]

        losses = []

        # action losses
        if isinstance(self.action_space, Discrete):
            action_loss = self._masked_cross_entropy_loss(
                preds[SampleBatch.ACTIONS], targets[SampleBatch.ACTIONS], masks
            )
        elif isinstance(self.action_space, Box):
            action_loss = self._masked_mse_loss(
                preds[SampleBatch.ACTIONS], targets[SampleBatch.ACTIONS], masks
            )
        else:
            raise NotImplementedError
        losses.append(action_loss)
        self.log("action_loss", action_loss)

        # obs losses
        if preds.get(SampleBatch.OBS) is not None:
            obs_loss = self._masked_mse_loss(
                preds[SampleBatch.OBS], targets[SampleBatch.OBS], masks
            )
            losses.append(obs_loss)
            self.log("obs_loss", obs_loss)

        # return to go losses
        if preds.get(SampleBatch.RETURNS_TO_GO) is not None:
            rtg_loss = self._masked_mse_loss(
                preds[SampleBatch.RETURNS_TO_GO],
                targets[SampleBatch.RETURNS_TO_GO],
                masks,
            )
            losses.append(rtg_loss)
            self.log("rtg_loss", rtg_loss)

        loss = sum(losses)
        return loss

    def _masked_cross_entropy_loss(
        self,
        preds: TensorType,
        targets: TensorType,
        masks: TensorType,
    ) -> TensorType:
        losses = F.cross_entropy(
            preds.reshape(-1, preds.shape[-1]), targets.reshape(-1), reduction="none"
        )
        losses = losses * masks.reshape(-1)
        return losses.mean()

    def _masked_mse_loss(
        self,
        preds: TensorType,
        targets: TensorType,
        masks: TensorType,
    ) -> TensorType:
        losses = F.mse_loss(preds, targets, reduction="none")
        losses = losses * masks.reshape(
            *preds.shape[0:2], *[1] * (len(preds.shape) - 2)
        )
        return losses.mean()

    def log(self, key, value):
        # internal log function
        self.model.tower_stats[key] = value

    @override(TorchPolicyV2)
    def stats_fn(self, train_batch: SampleBatch) -> Dict[str, TensorType]:
        stats_dict = {
            k: torch.stack(self.get_tower_stats(k)).mean().item()
            for k in self.model.tower_stats
        }
        return stats_dict


if __name__ == "__main__":

    obs_space = gym.spaces.Box(np.array((-1, -1)), np.array((1, 1)))
    act_space = gym.spaces.Box(np.array((-1, -1)), np.array((1, 1)))
    config = AlgorithmConfig().framework(framework="torch").to_dict()
    print(config["framework"])
    DTTorchPolicy(obs_space, act_space, config=config)
