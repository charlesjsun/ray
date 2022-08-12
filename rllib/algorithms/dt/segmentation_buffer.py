import logging
from collections import defaultdict
from typing import List

import numpy as np

from ray.rllib.policy.sample_batch import SampleBatch, concat_samples, MultiAgentBatch
from ray.rllib.utils.typing import SampleBatchType

logger = logging.getLogger(__name__)


class SegmentationBuffer:
    """A minimal replay buffer used by Decision Transformer (DT)
    to process episodes into max_seq_len length segments and do shuffling.
    """

    def __init__(
        self,
        capacity: int = 20,
        max_seq_len: int = 20,
        max_ep_len: int = 1000,
    ):
        """
        Args:
            capacity: Maximum number of episodes the buffer can store.
            max_seq_len: Length of segments that are sampled.
            max_ep_len: Maximum length of episodes added.
        """
        self.capacity = capacity
        self.max_seq_len = max_seq_len
        self.max_ep_len = max_ep_len

        self._buffer: List[SampleBatch] = []

    def add(self, batch: SampleBatch):
        """Add a SampleBatch of episodes. Replace if full.

        Args:
            batch: SampleBatch of full episodes.
        """
        episodes = batch.split_by_episode()
        for episode in episodes:
            self._add_single(episode)

    def _add_single(self, episode: SampleBatch):
        # Truncate if episode too long.
        # Note: sometimes this happens if the dataset shuffles such that the
        # same episode is concatenated together twice (which is okay).
        if episode.env_steps() > self.max_ep_len:
            logger.warning(
                f"The maximum rollout length is {self.max_ep_len} but we tried to add a"
                f"rollout of {episode.env_steps()} steps to the SegmentationBuffer. "
                f"This could be due to incorrect data in the dataset (need fixing), "
                f"or random shuffling that caused a duplicate rollout (no need to fix)."
            )
            episode = episode[: self.max_ep_len]

        if len(self._buffer) < self.capacity:
            self._buffer.append(episode)
        else:
            # TODO: replace proportional to episode length
            replace_ind = np.random.randint(0, self.capacity)
            self._buffer[replace_ind] = episode

    def sample(self, batch_size: int) -> SampleBatch:
        """Sample segments from the buffer.

        Args:
            batch_size: number of segments to sample.

        Returns:
            SampleBatch of segments with keys and shape {
                OBS: [batch_size, max_seq_len, obs_dim],
                ACTIONS: [batch_size, max_seq_len, act_dim],
                RETURNS_TO_GO: [batch_size, max_seq_len + 1, 1],
                T: [batch_size, max_seq_len],
                ATTENTION_MASKS: [batch_size, max_seq_len],
            }
        """
        samples = [self._sample_single() for _ in range(batch_size)]
        return concat_samples(samples)

    def _sample_single(self) -> SampleBatch:
        # TODO: sample proportional to episode length
        # Sample a random episode from the buffer and then sample a random
        # segment from that episode.
        buffer_ind = np.random.randint(0, len(self._buffer))
        episode = self._buffer[buffer_ind]
        ep_len = episode[SampleBatch.OBS].shape[0]
        # This offset accounts for either the normal case when max_seq_len is shorter
        # than the episode length, but also occasionally an episode will be shorter
        # than the context length (max_seq_len).
        offset = min(self.max_seq_len, ep_len)
        # We allow si to be negative (for now) because we want segments that only
        # contains the first few transitions (and padd the rest),
        # for example [0, 0, 0, 0, 0, 0, R0, s0, a0].
        si = np.random.randint(-offset + 1, ep_len - offset + 1)
        ei = si + offset
        # but for actual segmenting we don't want starting index to be negative.
        si = max(si, 0)

        obs = episode[SampleBatch.OBS][si:ei]
        actions = episode[SampleBatch.ACTIONS][si:ei]
        # Note that returns-to-go needs an extra elem as the target for the last action.
        returns_to_go = episode[SampleBatch.RETURNS_TO_GO][si : ei + 1].reshape(-1, 1)

        length = obs.shape[0]
        timesteps = np.arange(si, si + length, dtype=np.int32)
        masks = np.ones(length, dtype=returns_to_go.dtype)

        # Back pad returns-to-go with 0 if at end of rollout.
        if returns_to_go.shape[0] == length:
            returns_to_go = np.concatenate(
                [returns_to_go, np.zeros((1, 1), dtype=returns_to_go.dtype)], axis=0
            )

        # Front-pad if at beginning of rollout.
        pad_length = self.max_seq_len - length
        if pad_length > 0:
            obs = np.concatenate(
                [np.zeros((pad_length, *obs.shape[1:]), dtype=obs.dtype), obs], axis=0
            )
            actions = np.concatenate(
                [
                    np.zeros((pad_length, *actions.shape[1:]), dtype=actions.dtype),
                    actions,
                ],
                axis=0,
            )
            returns_to_go = np.concatenate(
                [np.zeros((pad_length, 1), dtype=returns_to_go.dtype), returns_to_go],
                axis=0,
            )
            timesteps = np.concatenate(
                [np.zeros(pad_length, dtype=timesteps.dtype), timesteps], axis=0
            )
            masks = np.concatenate(
                [np.zeros(pad_length, dtype=masks.dtype), masks], axis=0
            )

        assert obs.shape[0] == self.max_seq_len
        assert actions.shape[0] == self.max_seq_len
        assert timesteps.shape[0] == self.max_seq_len
        assert masks.shape[0] == self.max_seq_len
        assert returns_to_go.shape[0] == self.max_seq_len + 1

        return SampleBatch(
            **{
                SampleBatch.OBS: obs[None],
                SampleBatch.ACTIONS: actions[None],
                SampleBatch.RETURNS_TO_GO: returns_to_go[None],
                SampleBatch.T: timesteps[None],
                SampleBatch.ATTENTION_MASKS: masks[None],
            }
        )


class MultiAgentSegmentationBuffer:
    """A minimal replay buffer used by Decision Transformer (DT)
    to process episodes into max_seq_len length segments and do shuffling.
    Stores MultiAgentSample.
    """

    def __init__(
        self,
        capacity: int = 20,
        max_seq_len: int = 20,
        max_ep_len: int = 1000,
    ):
        """
        Args:
            capacity: Maximum number of episodes the buffer can store.
            max_seq_len: Length of segments that are sampled.
            max_ep_len: Maximum length of episodes added.
        """

        def new_buffer():
            return SegmentationBuffer(capacity, max_seq_len, max_ep_len)

        self.buffers = defaultdict(new_buffer)

    def add(self, batch: SampleBatchType):
        """Add a MultiAgentBatch of episodes. Replace if full.

        Args:
            batch: MultiAgentBatch of full episodes.
        """
        # Make a copy so the replay buffer doesn't pin plasma memory.
        batch = batch.copy()
        # Handle everything as if multi-agent.
        batch = batch.as_multi_agent()

        for policy_id, sample_batch in batch.policy_batches.items():
            self.buffers[policy_id].add(sample_batch)

    def sample(self, batch_size: int) -> MultiAgentBatch:
        """Sample segments from the buffer.

        Args:
            batch_size: number of segments to sample.

        Returns:
            MultiAgentBatch of segments with keys and shape {
                OBS: [batch_size, max_seq_len, obs_dim],
                ACTIONS: [batch_size, max_seq_len, act_dim],
                RETURNS_TO_GO: [batch_size, max_seq_len + 1, 1],
                T: [batch_size, max_seq_len],
                ATTENTION_MASKS: [batch_size, max_seq_len],
            }
        """
        samples = {}
        for policy_id, buffer in self.buffers.items():
            samples[policy_id] = buffer.sample(batch_size)
        return MultiAgentBatch(samples, batch_size)
