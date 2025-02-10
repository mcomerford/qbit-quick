def assert_called_once_with_in_any_order(mock, *args, **kwargs):
    """
    Custom matcher to check that the mock was called once with args that match,
    ignoring the order of lists/dictionaries passed in the call arguments.
    """
    # Retrieve the arguments from the mock call
    actual_call_args = mock.call_args[1]  # This gets the kwargs of the first call

    # Sort the actual and expected arguments to compare order-agnostic lists
    actual_hashes = sorted(actual_call_args.get('torrent_hashes', []))
    expected_hashes = sorted(kwargs.get('torrent_hashes', []))

    # Perform the assertion
    assert expected_hashes == actual_hashes, f'Expected {expected_hashes} but got {actual_hashes}'

    # Ensure that the mock was called exactly once
    mock.assert_called_once()
