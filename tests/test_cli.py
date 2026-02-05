# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Tests for the CLI module."""

import pytest
from argparse import Action
from io import StringIO
from unittest.mock import patch, MagicMock

from hsync_client.cli import SortedHelpFormatter, create_argument_parser


class TestSortedHelpFormatter:
    """Tests for SortedHelpFormatter class."""

    def test_sorts_actions_alphabetically(self):
        """Actions are sorted by their option strings."""
        formatter = SortedHelpFormatter(prog="test")
        
        # Create mock actions with different option strings
        action_z = MagicMock(spec=Action)
        action_z.option_strings = ["-z", "--zebra"]
        
        action_a = MagicMock(spec=Action)
        action_a.option_strings = ["-a", "--apple"]
        
        action_m = MagicMock(spec=Action)
        action_m.option_strings = ["-m", "--mango"]
        
        # Mock the parent add_arguments to capture the sorted order
        sorted_actions = []
        original_add_arguments = super(SortedHelpFormatter, formatter).add_arguments
        
        with patch.object(
            SortedHelpFormatter.__bases__[0],
            'add_arguments',
            side_effect=lambda actions: sorted_actions.extend(actions)
        ):
            formatter.add_arguments([action_z, action_a, action_m])
        
        # Verify actions are sorted alphabetically
        assert sorted_actions[0].option_strings == ["-a", "--apple"]
        assert sorted_actions[1].option_strings == ["-m", "--mango"]
        assert sorted_actions[2].option_strings == ["-z", "--zebra"]

    def test_positional_arguments_sorted_first(self):
        """Actions without option strings (positional) come first."""
        formatter = SortedHelpFormatter(prog="test")
        
        # Create positional action (no option strings)
        action_positional = MagicMock(spec=Action)
        action_positional.option_strings = []
        
        action_opt = MagicMock(spec=Action)
        action_opt.option_strings = ["-a", "--arg"]
        
        sorted_actions = []
        with patch.object(
            SortedHelpFormatter.__bases__[0],
            'add_arguments',
            side_effect=lambda actions: sorted_actions.extend(actions)
        ):
            formatter.add_arguments([action_opt, action_positional])
        
        # Positional (empty tuple) sorts before any option strings
        assert sorted_actions[0].option_strings == []
        assert sorted_actions[1].option_strings == ["-a", "--arg"]


class TestCreateArgumentParser:
    """Tests for create_argument_parser function."""

    def test_parser_created_successfully(self):
        """Parser is created without errors."""
        parser = create_argument_parser()
        assert parser is not None
        assert parser.prog == "hsync"

    def test_parser_has_required_bucket_argument(self):
        """Parser requires -b/--bucket argument."""
        parser = create_argument_parser()

        # Should fail without bucket
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_parser_accepts_bucket_short_option(self):
        """Parser accepts -b bucket option."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'testbucket'])
        assert args.bucket == 'testbucket'

    def test_parser_accepts_bucket_long_option(self):
        """Parser accepts --bucket option."""
        parser = create_argument_parser()
        args = parser.parse_args(['--bucket', 'mybucket123'])
        assert args.bucket == 'mybucket123'

    def test_parser_rejects_invalid_bucket_name(self):
        """Parser rejects bucket names with non-alphanumeric chars."""
        parser = create_argument_parser()
        # Bucket names with hyphens are not allowed
        with pytest.raises(SystemExit):
            parser.parse_args(['-b', 'test-bucket'])

    def test_server_not_required_at_parse_time(self):
        """Server argument is not required at parse time (validated later in main)."""
        parser = create_argument_parser()
        # Should NOT fail without server - validation happens in main()
        args = parser.parse_args(['-b', 'test'])
        assert args.bucket == 'test'
        assert args.server_url is None

    def test_server_can_be_provided(self):
        """Server argument can be provided via CLI."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '-s', 'http://test:8000'])
        assert args.bucket == 'test'
        assert args.server_url == 'http://test:8000'

    def test_watch_flag(self):
        """Parser accepts -w/--watch flag."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '-w'])
        assert args.continuous is True

    def test_verbose_flag(self):
        """Parser accepts -v/--verbose flag."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '-v'])
        assert args.debug is True

    def test_quiet_flag(self):
        """Parser accepts -q/--quiet flag."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '-q'])
        assert args.quiet is True

    def test_dry_run_flag(self):
        """Parser accepts -n/--dry-run flag."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '-n'])
        assert args.dryrun is True

    def test_insecure_flag(self):
        """Parser accepts --insecure flag."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '--insecure'])
        assert args.verify_ssl is False

    def test_no_color_flag(self):
        """Parser accepts --no-color flag."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '--no-color'])
        assert args.no_color is True

    def test_help_symbols_flag(self):
        """Parser accepts --help-symbols flag."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '--help-symbols'])
        assert args.legend is True

    def test_compare_by_choices(self):
        """Parser accepts only valid compare-by choices."""
        parser = create_argument_parser()

        # Valid choice: mtime
        args = parser.parse_args(['-b', 'test', '--compare-by', 'mtime'])
        assert args.sync_order == 'mtime'

        # Valid choice: ctime
        args = parser.parse_args(['-b', 'test', '--compare-by', 'ctime'])
        assert args.sync_order == 'ctime'

        # Invalid choice should fail
        with pytest.raises(SystemExit):
            parser.parse_args(['-b', 'test', '--compare-by', 'invalid'])

    def test_path_option(self):
        """Parser accepts -p/--path option."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '-p', '/tmp/sync'])
        assert args.local_path == '/tmp/sync'

    def test_ca_cert_option(self):
        """Parser accepts --ca-cert option."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '--ca-cert', '/path/to/ca.crt'])
        assert args.ca_cert == '/path/to/ca.crt'

    def test_output_option(self):
        """Parser accepts -o/--output option."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '-o', '/tmp/log.txt'])
        assert args.logfile == '/tmp/log.txt'

    def test_filter_path_option(self):
        """Parser accepts --filter-path option."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '--filter-path', 'subdir'])
        assert args.filter_path == 'subdir'

    def test_retry_delay_option(self):
        """Parser accepts --retry-delay option."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '--retry-delay', '10'])
        assert args.reconnect_delay == 10

    def test_default_values(self):
        """Parser sets correct default values."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test'])

        assert args.continuous is False
        assert args.debug is False
        assert args.quiet is False
        assert args.dryrun is False
        assert args.verify_ssl is True  # Default is True
        assert args.sync_order == 'mtime'
        assert args.reconnect_delay == 5
        assert args.ca_cert is None
        assert args.filter_path is None
        assert args.logfile is None
        assert args.resync is None
        assert args.env_file is None  # New --env-file parameter

    def test_uses_sorted_help_formatter(self):
        """Parser uses SortedHelpFormatter."""
        parser = create_argument_parser()
        assert parser.formatter_class == SortedHelpFormatter

    def test_env_file_option(self):
        """Parser accepts --env-file option."""
        parser = create_argument_parser()
        args = parser.parse_args(['-b', 'test', '--env-file', '/path/to/env'])
        assert args.env_file == '/path/to/env'

