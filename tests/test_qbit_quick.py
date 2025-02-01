import contextlib
import json
from io import StringIO
from unittest import TestCase, mock
from unittest.mock import patch, mock_open, Mock, MagicMock, ANY

from qbittorrentapi import Client, TorrentInfoList, TorrentDictionary, TorrentsAPIMixIn

import qbitquick.qbit_quick

# NOTE: The order of the @patch decorators is important. The first decorator refers to the last argument.
class Test(TestCase):

    @patch('qbitquick.qbit_quick.os')
    def test_default_config_is_not_created_when_no_args_are_passed_in(self, mock_os):
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            with self.assertRaises(SystemExit) as cm:
                qbitquick.qbit_quick.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn('usage:', temp_stdout.getvalue().strip())
            mock_os.assert_not_called()

    @patch('qbitquick.qbit_quick.os')
    @patch.object(qbitquick.qbit_quick.sys, 'argv', ['main', 'race'])
    def test_default_config_is_not_created_when_incomplete_args_are_passed_in(self, mock_os):
        with contextlib.redirect_stderr(StringIO()) as temp_stderr:
            with self.assertRaises(SystemExit) as cm:
                qbitquick.qbit_quick.main()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn('the following arguments are required: torrent_hash', temp_stderr.getvalue().strip())
            mock_os.assert_not_called()

    @patch('qbitquick.qbit_quick.Client')
    @patch('qbitquick.qbit_quick.shutil')
    @patch('qbitquick.qbit_quick.os')
    @patch.object(qbitquick.qbit_quick.sys, 'argv', ['main', 'race', 'hash'])
    def test_default_config_is_created_if_one_does_not_exist(self, mock_os, mock_shutil, mock_client):
        mock_os.path.exists.return_value = False
        mock_torrent = MagicMock()
        mock_torrent.hash = "hash"
        mocked_torrent_list = TorrentInfoList([mock_torrent])
        mock_client().torrents_info.return_value = mocked_torrent_list
        # mock_client.return_value.torrents_info.return_value = TorrentInfoList([{'hash': '{}'}])
        with patch("builtins.open", mock_open(read_data="{}")):
            qbitquick.qbit_quick.main()
            mock_shutil.copyfile.assert_called_with('./default_config.json', ANY)
            mock_client.torrents_info.remove('hash').assert_not_called()

    @patch('qbitquick.qbit_quick.Client')
    @patch('qbitquick.qbit_quick.shutil')
    @patch('qbitquick.qbit_quick.os')
    @patch.object(qbitquick.qbit_quick.sys, 'argv', ['main', 'race', 'hash'])
    def test_race(self, mock_os, mock_shutil, mock_client):
        mock_os.path.exists.return_value = False
        mock_client.return_value.torrents_info.return_value = TorrentInfoList([{'hash': '{}'}])
        with patch("builtins.open", mock_open(read_data="{}")):
            qbitquick.qbit_quick.main()
            mock_shutil.copyfile.assert_called_with('./default_config.json', ANY)
            mock_client.torrents_info.remove('hash').assert_not_called()
