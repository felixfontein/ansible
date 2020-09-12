"""Sanity test using validate-modules."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json
import os

from collections import defaultdict

# from ansible.constants import DOCUMENTABLE_PLUGINS
DOCUMENTABLE_PLUGINS = ('become', 'cache', 'callback', 'cliconf', 'connection', 'httpapi', 'inventory', 'lookup', 'module', 'netconf', 'shell', 'strategy', 'vars')

from .. import types as t

from ..sanity import (
    SanitySingleVersion,
    SanityMessage,
    SanityFailure,
    SanitySuccess,
    SANITY_ROOT,
)

from ..target import (
    TestTarget,
)

from ..util import (
    SubprocessError,
    display,
    find_python,
)

from ..util_common import (
    run_command,
)

from ..ansible_util import (
    ansible_environment,
    get_collection_detail,
    CollectionDetailError,
)

from ..config import (
    SanityConfig,
)

from ..ci import (
    get_ci_provider,
)

from ..data import (
    data_context,
)


def _get_plugin_type_getter():
    content = data_context().content
    prefixes = {
        plugin_type: content.plugin_paths.get(plugin_type) + '/'
        for plugin_type in DOCUMENTABLE_PLUGINS
        if plugin_type != 'module'
    }
    exceptions = set()
    for prefix in prefixes.values():
        exceptions.add(prefix + '__init__.py')
    if not data_context().content.collection:
        exceptions.add('lib/ansible/plugins/cache/base.py')

    def get_plugin_type(target):  # type: TestTarget -> t.Optional[str]
        if target.path in exceptions:
            return None
        if target.module:
            return 'module'
        for plugin_type, prefix in prefixes.items():
            if target.path.startswith(prefix):
                return plugin_type
        return None

    return get_plugin_type


class ValidateModulesTest(SanitySingleVersion):
    """Sanity test using validate-modules."""

    def __init__(self):
        super(ValidateModulesTest, self).__init__()
        self.optional_error_codes.update([
            'deprecated-date',
        ])

    @property
    def error_code(self):  # type: () -> t.Optional[str]
        """Error code for ansible-test matching the format used by the underlying test program, or None if the program does not use error codes."""
        return 'A100'

    def filter_targets(self, targets):  # type: (t.List[TestTarget]) -> t.List[TestTarget]
        """Return the given list of test targets, filtered to include only those relevant for the test."""
        get_plugin_type = _get_plugin_type_getter()
        return [target for target in targets if get_plugin_type(target) is not None]

    def test(self, args, targets, python_version):
        """
        :type args: SanityConfig
        :type targets: SanityTargets
        :type python_version: str
        :rtype: TestResult
        """
        env = ansible_environment(args, color=False)

        settings = self.load_processor(args)

        get_plugin_type = _get_plugin_type_getter()
        target_per_type = defaultdict(list)
        for target in targets.include:
            target_per_type[get_plugin_type(target)].append(target)

        python = find_python(python_version)

        cmd = [
            python,
            os.path.join(SANITY_ROOT, 'validate-modules', 'validate-modules'),
            '--format', 'json',
            '--arg-spec',
        ]

        if data_context().content.collection:
            cmd.extend(['--collection', data_context().content.collection.directory])

            try:
                collection_detail = get_collection_detail(args, python)

                if collection_detail.version:
                    cmd.extend(['--collection-version', collection_detail.version])
                else:
                    display.warning('Skipping validate-modules collection version checks since no collection version was found.')
            except CollectionDetailError as ex:
                display.warning('Skipping validate-modules collection version checks since collection detail loading failed: %s' % ex.reason)
        else:
            base_branch = args.base_branch or get_ci_provider().get_base_branch()

            if base_branch:
                cmd.extend([
                    '--base-branch', base_branch,
                ])
            else:
                display.warning('Cannot perform module comparison against the base branch because the base branch was not detected.')

        errors = []
        for plugin_type, plugin_targets in sorted(target_per_type.items()):
            paths = [target.path for target in plugin_targets]
            plugin_cmd = list(cmd)
            if plugin_type != 'module':
                plugin_cmd += ['--plugin-type', plugin_type]
            plugin_cmd += paths

            try:
                stdout, stderr = run_command(args, plugin_cmd, env=env, capture=True)
                status = 0
            except SubprocessError as ex:
                stdout = ex.stdout
                stderr = ex.stderr
                status = ex.status

            if stderr or status not in (0, 3):
                raise SubprocessError(cmd=plugin_cmd, status=status, stderr=stderr, stdout=stdout)

            if args.explain:
                continue

            messages = json.loads(stdout)

            plugin_errors = []
            for filename in messages:
                output = messages[filename]

                for item in output['errors']:
                    plugin_errors.append(SanityMessage(
                        path=filename,
                        line=int(item['line']) if 'line' in item else 0,
                        column=int(item['column']) if 'column' in item else 0,
                        level='error',
                        code='%s' % item['code'],
                        message=item['msg'],
                    ))

            plugin_errors = settings.process_errors(plugin_errors, paths)
            errors += plugin_errors

        if args.explain:
            return SanitySuccess(self.name)

        if errors:
            return SanityFailure(self.name, messages=errors)

        return SanitySuccess(self.name)
