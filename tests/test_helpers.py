def sort_args(args, kwargs):
    """ Recursively sort lists/dictionaries to make comparison order-agnostic. """

    def sort_if_needed(value):
        if isinstance(value, list) or isinstance(value, set):
            return sorted(value)
        if isinstance(value, dict):
            return {k: sort_if_needed(v) for k, v in sorted(value.items())}
        return value

    return tuple(sort_if_needed(arg) for arg in args), {k: sort_if_needed(v) for k, v in kwargs.items()}


def assert_called_once_with_in_any_order(mock, *expected_args, **expected_kwargs):
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


def merge_and_remove(original, updates):
    """
    Updates `original` in place by merging values from `updates`.
    If a key in `updates` has a value of None, it will be removed from `original`.
    """
    for key, value in updates.items():
        if value is None:
            original.pop(key, None)  # Remove key if it exists
        else:
            original[key] = value  # Update or add key-value pair
