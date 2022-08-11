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
    TYPE_CHECKING,
)

import tree
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
from ray.rllib.policy.torch_mixins import LearningRateSchedule
from ray.rllib.policy.torch_policy_v2 import TorchPolicyV2
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.utils.framework import try_import_torch
from ray.rllib.utils.annotations import override, PublicAPI
from ray.rllib.utils.numpy import convert_to_numpy
from ray.rllib.utils.torch_utils import apply_grad_clipping
from ray.rllib.utils.typing import (
    TrainerConfigDict,
    TensorType,
    TensorStructType,
)

if TYPE_CHECKING:
    from ray.rllib.evaluation import Episode  # noqa

torch, nn = try_import_torch()
F = nn.functional


class DTTorchPolicy(LearningRateSchedule, TorchPolicyV2):
    """ """

    def __init__(
        self,
        observation_space: gym.spaces.Space,
        action_space: gym.spaces.Space,
        config: TrainerConfigDict,
    ):
        """ """
        print("DT Policy Config")
        import pprint

        pprint.pprint(config)

        LearningRateSchedule.__init__(
            self,
            config["lr"],
            config["lr_schedule"],
        )

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
                max_ep_len=self.config["horizon"],
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
            weight_decay=self.config["optimizer"]["weight_decay"],
            betas=self.config["optimizer"]["betas"],
        )

        return optimizer

    @override(TorchPolicyV2)
    def postprocess_trajectory(
        self,
        sample_batch: SampleBatch,
        other_agent_batches: Optional[Dict[Any, SampleBatch]] = None,
        episode: Optional["Episode"] = None,
    ) -> SampleBatch:
        """Called by offline data reader after loading in one episode.
        Calculates returns-to-go.
        """
        assert len(sample_batch.split_by_episode()) == 1

        rewards = sample_batch[SampleBatch.REWARDS].reshape(-1)
        sample_batch[SampleBatch.RETURNS_TO_GO] = discount_cumsum(rewards, 1.0)

        return sample_batch

    @PublicAPI
    def get_initial_input_dict(self, observation: TensorStructType) -> SampleBatch:
        """Get the initial input_dict to be passed into compute_single_action.

        Args:
            observation: first (unbatched) observation from env.reset()

        Returns:
            The input_dict for inference.
        """
        observation = convert_to_numpy(observation)
        obs_shape = observation.shape
        obs_dtype = observation.dtype

        act_shape = self.action_space.shape
        act_dtype = self.action_space.dtype

        # Here we will pad observations to batch size 1
        # and seq_len according to view_requirements.

        observations = np.concatenate(
            [
                np.zeros((self.max_seq_len - 1, *obs_shape), dtype=obs_dtype),
                observation[None],
            ],
            axis=0,
        )

        actions = np.zeros((self.max_seq_len - 1, *act_shape), dtype=act_dtype)

        rtg = np.zeros(self.max_seq_len - 1, dtype=np.float32)

        rewards = np.zeros((), dtype=np.float32)

        timesteps = np.full(self.max_seq_len - 1, fill_value=-1, dtype=np.int32)

        input_dict = SampleBatch(
            {
                SampleBatch.OBS: observations,
                SampleBatch.ACTIONS: actions,
                SampleBatch.RETURNS_TO_GO: rtg,
                SampleBatch.REWARDS: rewards,
                SampleBatch.T: timesteps,
            }
        )
        return input_dict

    @PublicAPI
    def get_next_input_dict(
        self,
        input_dict: SampleBatch,
        action: TensorStructType,
        reward: TensorStructType,
        next_obs: TensorStructType,
        extra: Dict[str, TensorType],
    ) -> SampleBatch:
        """Returns a new input_dict after stepping through the environment once.

        Args:
            input_dict: the input dict passed into compute_single_action.
            action: the (unbatched) action taken this step.
            reward: the (unbatched) reward from env.step
            next_obs: the (unbatached) next observation from env.step
            extra: the extra action out from compute_single_action.
                In this case contains current returns to go *before* the current
                reward is subtracted from target_return.

        Returns:
            A new input_dict to be passed into compute_single_action.
        """
        # creates a copy of input_dict with only numpy arrays
        input_dict = tree.map_structure(convert_to_numpy, input_dict)
        # convert everything else to numpy as well
        action, reward, next_obs, extra = convert_to_numpy(
            (action, reward, next_obs, extra)
        )

        input_dict[SampleBatch.OBS] = np.concatenate(
            [
                input_dict[SampleBatch.OBS][1:],
                next_obs[None],
            ],
            axis=0,
        )

        input_dict[SampleBatch.ACTIONS] = np.concatenate(
            [
                input_dict[SampleBatch.ACTIONS][1:],
                action[None],
            ],
            axis=0,
        )

        input_dict[SampleBatch.REWARDS] = np.asarray(reward)

        input_dict[SampleBatch.RETURNS_TO_GO] = np.concatenate(
            [
                input_dict[SampleBatch.RETURNS_TO_GO][1:],
                np.asarray(extra[SampleBatch.RETURNS_TO_GO])[None],
            ],
            axis=0,
        )

        input_dict[SampleBatch.T] = np.concatenate(
            [
                input_dict[SampleBatch.T][1:],
                input_dict[SampleBatch.T][-1:] + 1,
            ],
            axis=0,
        )

        return input_dict

    @override(TorchPolicyV2)
    def action_distribution_fn(
        self,
        model: ModelV2,
        *,
        obs_batch: SampleBatch,
        state_batches: TensorType,
        **kwargs,
    ) -> Tuple[TensorType, type, List[TensorType]]:
        """
        Note: This method is only ran during evaluation and inference.

        Args:
            model: DTTorchModel of this policy.
            obs_batch: input_dict (that contains a batch dimension for each value).
                This is modified to be used by a subsequent call to
                extra_action_out.
            state_batches: RNN states (not used).
            kwargs: forward compatibility (unused) kwargs.

        Returns:
            A tuple of: (
                batched input to the action distribution clas,
                the action distribution class,
                updated RNN state (unused),
            )
        """
        # Note: this doesn't create a new SampleBatch, so changes to obs_batch persists
        obs_batch = self._lazy_tensor_dict(obs_batch)

        batch_size = obs_batch[SampleBatch.OBS].shape[0]

        # NOTE: This is probably the most confusing part of the code, made to work with
        # env_runner and SimpleListCollector during evaluation, and thus should
        # be changed for the new Policy and Connector API.
        # So I'll explain how it works.

        # Add current timestep (+1 because -1 is first observation)
        # NOTE: ViewRequirement of timestep is -(max_seq_len-2):0.
        # The wierd limits is because RLlib treats initial obs as time -1,
        # and then 0 is (act, rew, next_obs), etc.
        # So we only collect max_seq_len-1 from the rollout and create the current
        # step here by adding 1.
        # Decision transformer treats initial observation as timestep 0, giving us
        # 0 is (obs, act, rew).
        timesteps = obs_batch[SampleBatch.T]
        new_timestep = timesteps[:, -1:] + 1
        obs_batch[SampleBatch.T] = torch.cat([timesteps, new_timestep], dim=1)

        # mask out any padded value at start of rollout
        # NOTE: the other reason for doing this is that evaluation rollout front
        # pads timesteps with -1, so using this we can find out when we need to mask
        # out the front section of the batch.
        obs_batch[SampleBatch.ATTENTION_MASKS] = torch.where(
            obs_batch[SampleBatch.T] >= 0, 1.0, 0.0
        )

        # Remove out-of-bound -1 timesteps after attention mask is calculated
        uncliped_timesteps = obs_batch[SampleBatch.T]
        obs_batch[SampleBatch.T] = torch.where(
            uncliped_timesteps < 0,
            torch.zeros_like(uncliped_timesteps),
            uncliped_timesteps,
        )

        # Computes returns-to-go.
        # NOTE: There are two rtg calculations: updated_rtg and initial_rtg.
        # updated_rtg takes the previous rtg value (the ViewRequirement is
        # -(max_seq_len-1):-1), and subtracts the last reward from it.
        rtg = obs_batch[SampleBatch.RETURNS_TO_GO]
        last_rtg = rtg[:, -1]
        last_reward = obs_batch[SampleBatch.REWARDS]
        updated_rtg = last_rtg - last_reward
        # initial_rtg simply is filled with target_return.
        # These two are both only for the current timestep.
        initial_rtg = torch.full(
            (batch_size, 1),
            fill_value=self.config["target_return"],
            dtype=rtg.dtype,
            device=rtg.device,
        )

        # Then based on weather we are currently at the first timestep or
        new_rtg = torch.where(new_timestep == 0, initial_rtg, updated_rtg[:, None])
        obs_batch[SampleBatch.RETURNS_TO_GO] = torch.cat([rtg, new_rtg], dim=1)[
            ..., None
        ]

        # Pad current action (is not actually attended to and used during inference)
        actions = obs_batch[SampleBatch.ACTIONS]
        action_pad = torch.zeros(
            (batch_size, 1, *actions.shape[2:]),
            dtype=actions.dtype,
            device=actions.device,
        )
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
                preds[SampleBatch.RETURNS_TO_GO] * 0.001,
                targets[SampleBatch.RETURNS_TO_GO] * 0.001,
                masks,
            )
            losses.append(rtg_loss)
            self.log("rtg_loss", rtg_loss)

        self.log("cur_lr", torch.tensor(self.cur_lr))

        loss = sum(losses)
        return loss

    def _masked_cross_entropy_loss(
        self,
        preds: TensorType,
        targets: TensorType,
        masks: TensorType,
    ) -> TensorType:
        """Computes cross-entropy loss between preds and targets, subject to a mask.

        Args:
            preds: logits of shape [B1, ..., Bn, M]
            targets: index targets for preds of shape [B1, ..., Bn]
            masks: 0 means don't compute loss, 1 means compute loss
                shape [B1, ..., Bn]

        Returns:
            Scalar cross entropy loss.
        """
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
        """Computes MSE loss between preds and targets, subject to a mask.

        Args:
            preds: logits of shape [B1, ..., Bn, M]
            targets: index targets for preds of shape [B1, ..., Bn]
            masks: 0 means don't compute loss, 1 means compute loss
                shape [B1, ..., Bn]

        Returns:
            Scalar cross entropy loss.
        """
        losses = F.mse_loss(preds, targets, reduction="none")
        losses = losses * masks.reshape(
            *preds.shape[0:2], *[1] * (len(preds.shape) - 2)
        )
        return losses.mean()

    @override(TorchPolicyV2)
    def extra_grad_process(self, local_optimizer, loss):
        return apply_grad_clipping(self, local_optimizer, loss)

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
