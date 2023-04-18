import numpy as np
import logging

from .playlist import Playlist
from . import load
from . import mmr


logger = logging.getLogger(__name__)


def filter_meta_score_info_below(at_or_below):
    def filter_by_score(status):
        return status.score_info.meta_score > at_or_below
    return filter_by_score


class ReplaySetAssesor:

    class ReplayStatus:
        ready = None

    class ScoreInfoStatus(ReplayStatus):

        def __init__(self, score_info):
            self.score_info = score_info

        @property
        def ready(self):
            return not any(mmr is None for _, mmr in self.score_info.estimates)

    class FailedStatus(ReplayStatus, Exception):
        ready = False

        def __init__(self, exception):
            self.exception = exception

    class TensorFail(FailedStatus):
        pass

    class MetaFail(FailedStatus):
        pass

    def __init__(
            self, replay_set: load.ReplaySet, scorer,
            playlist=Playlist.DOUBLES
    ):
        self._replay_set = replay_set
        self._scorer = scorer
        self._playlist = Playlist(playlist)

    def get_replay_statuses(self, load_tensor=True):
        return {
            uuid: self._get_replay_status(uuid, load_tensor=load_tensor)
            for uuid in self._replay_set.get_replay_uuids()
        }

    def get_replay_statuses_by_rank(self, load_tensor=True):
        replay_statuses = self.get_replay_statuses(load_tensor=load_tensor)
        results = {"Failed": {}}
        for rank in mmr.rank_number_to_name.values():
            results[rank] = {}
        for uuid, status in replay_statuses.items():
            if status.ready:
                mmrs = [
                    mmr for _, mmr in status.score_info.estimates
                ]
                rank = mmr.playlist_to_converter[self._playlist].get_rank_name(
                    np.mean(mmrs)
                )
                results[rank][uuid] = status
            else:
                results["Failed"][uuid] = status
        return results

    def get_top_scoring_n_replay_per_rank(
            self, count_per_rank, filter_function=filter_meta_score_info_below(0)
    ):
        replay_statuses = self.get_replay_statuses_by_rank()
        top_replays = {}
        for rank, uuid_to_status in replay_statuses.items():
            if not isinstance(rank, mmr.Rank):
                continue
            replay_pairs = [
                pair for pair in uuid_to_status.items()
                if filter_function(pair[1])
            ]
            replay_pairs.sort(key=lambda pair: pair[1].score_info.meta_score, reverse=True)
            if len(replay_pairs) < count_per_rank:
                logger.warning(
                    f"Could only produce {len(replay_pairs)} "
                    f"of the {count_per_rank} requested for {rank}"
                )
            top_replays[rank] = replay_pairs[:count_per_rank]
        return top_replays

    known_errors = [
        "ActorId(-1) not found",
        "Player team unknown",
        "Players found in frames that were not part of",
        "Replay is corrupt",
        "Could not decode replay content data at offset",
        "Could not find actor for"
    ]

    def _should_reraise(self, e):
        try:
            exception_text = e.args[0]
        except Exception:
            pass
        else:
            for error_text in self.known_errors:
                if error_text in exception_text:
                    return False
        return True

    def _get_replay_status(self, uuid, load_tensor=True):
        logger.info(self._replay_set.replay_path(uuid))
        if (
                isinstance(self._replay_set, load.CachedReplaySet) and not
                load_tensor and self._replay_set.is_cached(uuid)
        ):
            meta = self._replay_set.get_replay_meta(uuid)
        else:
            try:
                _, meta = self._replay_set.get_replay_tensor(uuid)
            except Exception as e:
                logger.warn(f"Tensor load failure for {uuid}, {e}")
                if self._should_reraise(e):
                    raise e
                else:
                    return self.TensorFail(e)

        score_info = self._scorer.score_replay_meta(meta, playlist=self._playlist)
        score, estimates, scores = score_info

        return self.ScoreInfoStatus(score_info)

    def _check_labels(self, meta):
        try:
            return self._get_player_labels(meta)
        except self.LabelFail as e:
            logger.warn(f"Label failure for {meta}, {e}")
            return e


def get_passed_stats(statuses_by_rank):
    return {
        rank: len(statuses)
        for rank, statuses in statuses_by_rank.items()
    }