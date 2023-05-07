"""Load replays into memory into a format that can be used with torch."""
import abc
import json
import logging
import os
import torch
import boxcars_py

from pathlib import Path
from torch.utils.data import Dataset

from . import util
from ._replay_meta import ReplayMeta


logger = logging.getLogger(__name__)


def get_meta_boxcars(_, filepath):
    return ReplayMeta.from_boxcar_frames_meta(
        boxcars_py.get_replay_meta(filepath)['Ok']['replay_meta']
    )


class ReplaySet(abc.ABC):
    """Abstract base class for objects representing a collection of replays."""

    @classmethod
    def cached(cls, cache_directory, *args, **kwargs):
        """Return a cached version of this the replay set."""
        return CachedReplaySet(cls(*args, **kwargs), cache_directory, **kwargs)

    @abc.abstractmethod
    def get_replay_uuids(self) -> [str]:
        """Get the replay uuids that are a part of this dataset."""
        pass

    @abc.abstractmethod
    def get_replay_tensor(self, uuid) -> (torch.Tensor, ReplayMeta):
        """Get the replay tensor and player order associated with the provided uuid."""
        pass


class CachedReplaySet(ReplaySet):
    """Wrapper for a replay set that caches the tensors that are provided by `get_replay_tensor`."""

    _meta_extension = "replay_meta"

    def __init__(
            self, replay_set: ReplaySet, cache_directory: Path, cache_extension="pt",
            backup_get_meta=get_meta_boxcars, ensure_bcf_arg_match=True, **kwargs
    ):
        """Initialize the cached replay set."""
        self._replay_set = replay_set
        self._cache_directory = cache_directory
        self._cache_extension = cache_extension
        self._backup_get_meta = backup_get_meta
        self._boxcar_frames_arguments = kwargs.get("boxcar_frames_arguments", {})
        if not os.path.exists(self._cache_directory):
            os.makedirs(self._cache_directory)

        if ensure_bcf_arg_match:
            self._check_boxcar_frames_arguments_match()

    @property
    def _boxcar_frames_arguments_path(self):
        return os.path.join(self._cache_directory, ".boxcars_frames_arguments")

    def _check_boxcar_frames_arguments_match(self):
        if len(os.listdir(self._cache_directory)) == 0:
            with open(self._boxcar_frames_arguments_path, 'w') as f:
                f.write(json.dumps(self._boxcar_frames_arguments))
        elif os.path.exists(self._boxcar_frames_arguments_path):
            with open(self._boxcar_frames_arguments_path, 'r') as f:
                existing_arguments = json.loads(f.read())
                if existing_arguments != self._boxcar_frames_arguments:
                    raise Exception(
                        f"Existing boxcar frames arguments {existing_arguments} "
                        f"did not match current {self._boxcar_frames_arguments}."
                    )
        else:
            logger.warn(
                f"No record of boxcar_frames_arguments in cache_directory "
                f"{self._cache_directory}"
            )

    def get_replay_uuids(self):
        """Get the replay uuids that are a part of this dataset."""
        return self._replay_set.get_replay_uuids()

    def get_replay_tensor_with_headers(self, uuid):
        return self._replay_set.get_replay_tensor_with_headers(uuid)

    def _cache_path_for_replay_with_extension(self, replay_id, extension):
        return os.path.join(self._cache_directory, f"{replay_id}.{extension}")

    def _get_tensor_and_meta_path(self, uuid):
        tensor_path = self._cache_path_for_replay_with_extension(uuid, self._cache_extension)
        meta_path = self._cache_path_for_replay_with_extension(uuid, self._meta_extension)
        return tensor_path, meta_path

    def _maybe_load_from_cache(self, uuid):
        tensor_path, meta_path = self._get_tensor_and_meta_path(uuid)
        tensor_present = os.path.exists(tensor_path)
        meta_present = os.path.exists(meta_path)
        if tensor_present and meta_present:
            logger.debug(f"Cache hit for {uuid}")
            with open(tensor_path, 'rb') as f:
                tensor = torch.load(f)
            with open(meta_path, 'rb') as f:
                meta = ReplayMeta.from_dict(json.loads(f.read()))
            return tensor, meta
        elif tensor_present != meta_present:
            logger.warn(
                f"{tensor_path} exists: {tensor_present} "
                f"meta_present, {meta_path} exists: {meta_present}"
            )
            if not meta_present:
                # XXX: this is not great since its assuming directory replay set
                try:
                    replay_filepath = self._replay_set.replay_path(uuid)
                except Exception as e:
                    logger.warn(f"Error trying to get only player meta {e}")
                    return None
                else:
                    meta = self._backup_get_meta(uuid, replay_filepath)
                    if meta is not None:
                        self._json_dump_meta(meta, meta_path)
                        return self._maybe_load_from_cache(uuid)

    def _save_to_cache(self, replay_id, replay_data, meta):
        tensor_path, meta_path = self._get_tensor_and_meta_path(replay_id)
        with open(tensor_path, 'wb') as f:
            torch.save(replay_data, f)
        self._json_dump_meta(meta, meta_path)
        return replay_data, meta

    def _json_dump_meta(self, meta: ReplayMeta, path):
        with open(path, 'w') as f:
            f.write(json.dumps(meta.to_dict()))

    def get_replay_meta(self, uuid) -> ReplayMeta:
        meta_path = self._cache_path_for_replay_with_extension(uuid, self._meta_extension)
        with open(meta_path, 'rb') as f:
            return ReplayMeta.from_dict(json.loads(f.read()))

    def get_replay_tensor(self, uuid) -> (torch.Tensor, ReplayMeta):
        """Get the replay tensor and player meta associated with the provided uuid."""
        return self._maybe_load_from_cache(uuid) or self._save_to_cache(
            uuid, *self._replay_set.get_replay_tensor(uuid)
        )

    def is_cached(self, uuid) -> bool:
        tensor_path, meta_path = self._get_tensor_and_meta_path(uuid)
        return os.path.exists(tensor_path) and os.path.exists(meta_path)

    def __getattr__(self, name):
        """Defer to self._replay_set."""
        return getattr(self._replay_set, name)

    def __getitem__(self, index):
        return self._replay_set[index]


class DirectoryReplaySet(ReplaySet):
    """A replay set that consists of replay files in a potentially nested directory."""

    def __init__(
            self, filepath, replay_extension="replay", boxcar_frames_arguments=None,
    ):
        self._filepath = filepath
        self._replay_extension = replay_extension
        self._replay_id_paths = list(self._get_replay_ids())
        self._replay_path_dict = dict(self._replay_id_paths)
        self._boxcar_frames_arguments = boxcar_frames_arguments or {}

    def _get_replay_ids(self):
        return util.get_replay_uuids_in_directory(
            self._filepath, replay_extension=self._replay_extension
        )

    def replay_path(self, replay_id):
        """Get the path of the given replay id."""
        return self._replay_path_dict[replay_id]

    def get_replay_uuids(self):
        return [replay_id for replay_id, _ in self._replay_id_paths]

    def get_replay_tensor(self, uuid) -> (torch.Tensor, ReplayMeta):
        """Get the replay tensor and player order associated with the provided uuid."""
        replay_path = self.replay_path(uuid)
        logger.info(f"Loading replay from {replay_path}")
        replay_meta, np_array = boxcars_py.get_ndarray_with_info_from_replay_filepath(
            replay_path, **self._boxcar_frames_arguments
        )
        return (
            torch.as_tensor(np_array),
            ReplayMeta.from_boxcar_frames_meta(replay_meta['replay_meta']),
        )

    def __getitem__(self, index):
        return self.get_replay_tensor(self._replay_id_paths[index][0])


class ReplayDataset(Dataset):
    """Load data from rocket league replay files in a directory."""

    def __init__(
            self, replay_set: ReplaySet, lookup_label, playlist,
            header_info, label_scaler=util.HorribleHackScaler,
            preload=False,
    ):
        """Initialize the data loader."""
        self._replay_set = replay_set
        self._replay_ids = list(replay_set.get_replay_uuids())
        self._playlist = playlist
        self._header_info = header_info
        self._lookup_label = lookup_label
        self._label_cache = {}
        self._label_scaler = label_scaler
        if preload:
            for i in range(len(self._replay_ids)):
                self[i]

    @property
    def features_per_frame(self):
        return util.feature_count_for(self._playlist, self._header_info)

    @property
    def label_count(self):
        return self._playlist.player_count

    def _get_replay_labels(self, uuid, meta):
        try:
            return self._label_cache[uuid]
        except KeyError:
            pass

        result = torch.FloatTensor([
            self._label_scaler.scale(self._lookup_label(player, meta.datetime))
            for player in meta.player_order
        ])

        if len(result) != self.label_count:
            raise Exception(f"Expected {self.label_count}, got {len(result)}")

        self._label_cache[uuid] = result
        return result

    def __len__(self):
        """Simply return the length of the replay ids calculated in init."""
        return len(self._replay_ids)

    def __getitem__(self, index):
        """Get the replay at the provided index."""
        return self.get_with_uuid(index)[1:]

    def get_with_uuid(self, index):
        if index < 0 or index > len(self._replay_ids):
            raise KeyError
        uuid = self._replay_ids[index]
        replay_tensor, meta = self._replay_set.get_replay_tensor(uuid)
        labels = self._get_replay_labels(uuid, meta)

        return uuid, replay_tensor, labels

    def iter_with_uuid(self):
        for i in range(len(self._replay_ids)):
            yield self.get_with_uuid(i)