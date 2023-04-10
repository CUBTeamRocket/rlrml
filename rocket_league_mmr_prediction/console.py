"""Defines command line entrypoints to the this library."""
import argparse
import asyncio
import coloredlogs
import functools
import logging
import os
import sys
import xdg_base_dirs

from pathlib import Path

from . import tracker_network
from . import player_cache as pc
from . import load
from . import migration
from . import logger
from . import mmr


def _add_rlrml_args(parser=None):
    parser = parser or argparse.ArgumentParser()

    rlrml_directory = os.path.join(xdg_base_dirs.xdg_data_dirs()[0])

    parser.add_argument(
        '--player-cache',
        help="The directory where the player cache can be found.",
        type=Path,
        default=os.path.join(rlrml_directory, "player_cache")
    )
    parser.add_argument(
        '--replay-path',
        help="The directory where game files are stored.",
        type=Path,
        default=os.path.join(rlrml_directory, "replays")
    )
    parser.add_argument(
        '--tensor-cache',
        help="The directory where the tensor cache is held",
        type=Path,
        default=os.path.join(rlrml_directory, "tensor_cache")
    )
    return parser


def _call_with_sys_argv(function):
    @functools.wraps(function)
    def call_with_sys_argv():
        coloredlogs.install(level='INFO', logger=logger)
        logger.setLevel(logging.INFO)
        function(*sys.argv[1:])
    return call_with_sys_argv


def load_game_dataset():
    """Convert the game provided through sys.argv."""
    coloredlogs.install(level='INFO', logger=logger)
    logger.setLevel(logging.INFO)
    parser = _add_rlrml_args()
    args = parser.parse_args()
    print(args)
    player_cache = pc.PlayerCache(str(args.player_cache))
    cached_player_get = pc.CachedGetPlayerData(
        player_cache, tracker_network.get_player_data_with_429_retry
    ).get_player_data
    assesor = load.ReplaySetAssesor(
        load.DirectoryReplaySet.cached(args.tensor_cache, args.replay_path),
        load.player_cache_label_lookup(cached_player_get)
    )
    result = assesor.get_replay_statuses()
    import ipdb; ipdb.set_trace()


@_call_with_sys_argv
def convert_game(filepath):
    game = load.get_carball_game(filepath)
    converter = load._CarballToTensorConverter(game).get_meta()


@_call_with_sys_argv
def load_game_at_indices(filepath, *indices):
    """Convert the game provided through sys.argv."""
    dataset = load.ReplayDataset(filepath, eager_labels=False)
    for index in indices:
        dataset[int(index)]


@_call_with_sys_argv
def fill_cache_with_tracker_rank(filepath):
    """Fill a player info cache in a directory of replays."""
    loop = asyncio.get_event_loop()
    task = migration.populate_player_cache_from_directory_using_tracker_network(filepath)
    loop.run_until_complete(task)


@_call_with_sys_argv
def _iter_cache(filepath):
    from . import util
    from . import player_cache as cache
    from . import tracker_network as tn
    import sdbus
    sdbus.set_default_bus(sdbus.sd_bus_open_system())

    old_form = []
    missing_data = 0
    present_data = 0
    player_cache = cache.PlayerCache.new_with_cache_directory(filepath)
    player_get = util.vpn_cycled_cached_player_get(filepath, player_cache=player_cache)
    for player_key, player_data in player_cache:
        if "__error__" in player_data:
            if player_data["__error__"]["type"] == "500":
                player_get({"__tracker_suffix__": player_key})
            missing_data += 1
        else:
            if "platform" not in player_data and "mmr" in player_data:
                print(f"Fixing {player_key}")
                combined = tn.combine_profile_and_mmr_json(player_data)
                player_cache.insert_data_for_player(
                    {"__tracker_suffix__": player_key}, combined
                )
            present_data += 1

    del player_cache

    print(f"present_data: {present_data}, missing_data: {missing_data}")

    if len(old_form):
        logger.warn(f"Non-empty old formm {old_form}")

    for player_suffix in old_form:
        player_get({"__tracker_suffix__": player_suffix})


@_call_with_sys_argv
def _copy_games(source, dest):
    import sdbus
    sdbus.set_default_bus(sdbus.sd_bus_open_system())
    migration.copy_games_if_metadata_available_and_conditions_met(source, dest)


@_call_with_sys_argv
def host_plots(filepath):
    """Run an http server that hosts plots of player mmr that in the cache."""
    from . import _http_graph_server
    _http_graph_server.make_routes(filepath)
    _http_graph_server.app.run(port=5001)


@_call_with_sys_argv
def get_player(filepath, player_key):
    """Get the provided player either from the cache or the tracker network."""
    import json
    import sdbus
    from . import util

    sdbus.set_default_bus(sdbus.sd_bus_open_system())
    player_get = util.vpn_cycled_cached_player_get(filepath)
    player = player_get({"__tracker_suffix__": player_key})
    # print(player["platform"])
    # print(len(player['mmr_history']['Ranked Doubles 2v2']))
    season_dates = mmr.tighten_season_dates(mmr.SEASON_DATES, move_end_date=1)
    # print(season_dates)
    segmented_history = mmr.split_mmr_history_into_seasons(
        player['mmr_history']['Ranked Doubles 2v2'],
        season_dates=season_dates
    )

    print(json.dumps(
        mmr.calculate_all_season_statistics(segmented_history, keep_poly=False)
    ))
