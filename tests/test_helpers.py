import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

from qbittorrentapi import TorrentDictionary, TorrentInfoList


def sort_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """ Recursively sort lists/dictionaries to make comparison order-agnostic. """

    def sort_if_needed(value: object) -> object:
        if isinstance(value, list) or isinstance(value, set):
            return sorted(value)
        if isinstance(value, dict):
            return {k: sort_if_needed(v) for k, v in sorted(value.items())}
        return value

    return tuple(sort_if_needed(arg) for arg in args), {k: sort_if_needed(v) for k, v in kwargs.items()}


def assert_called_once_with_in_any_order(mock: Mock, *expected_args: Any, **expected_kwargs: Any) -> None:
    """
    Custom matcher to check that the mock was called once with the expected arguments,
    ignoring the order of lists/dictionaries in the arguments.
    """
    mock.assert_called_once()

    actual_args, actual_kwargs = mock.call_args

    actual_args_sorted, actual_kwargs_sorted = sort_args(actual_args, actual_kwargs)
    expected_args_sorted, expected_kwargs_sorted = sort_args(expected_args, expected_kwargs)

    assert (actual_args_sorted, actual_kwargs_sorted) == (expected_args_sorted, expected_kwargs_sorted), \
        f"Expected {expected_args_sorted, expected_kwargs_sorted} but got {actual_args_sorted, actual_kwargs_sorted}"


def merge_and_remove(original: dict[str, Any], updates: dict[str, Any]) -> None:
    """
    Updates `original` in place by merging values from `updates`.
    If a key in `updates` has a value of None, it will be removed from `original`.
    """
    if not updates:
        return
    for key, value in updates.items():
        if value is None:
            original.pop(key, None)  # Remove key if it exists
        elif isinstance(value, dict):
            if key in original:
                merge_and_remove(original[key], value)
            else:
                original[key] = value
        else:
            original[key] = value  # Update or add key-value pair


def print_torrents(torrents: list[TorrentDictionary]) -> None:
    # Print out the torrents, so we have all the information if an assertion fails
    print(json.dumps(torrents, indent=2))


def calculate_last_activity_time(time_since_active: timedelta) -> int:
    return int((datetime.now(timezone.utc) - time_since_active).timestamp())


def mock_torrents_info(mock_client_instance: Mock, torrents: list[TorrentDictionary]) -> None:
    def _side_effect(**kwargs: dict[str, Any]) -> TorrentInfoList:
        if kwargs:
            return TorrentInfoList([t for t in torrents if t.hash in kwargs["torrent_hashes"]])
        return TorrentInfoList(torrents)

    mock_client_instance.torrents_info.side_effect = _side_effect