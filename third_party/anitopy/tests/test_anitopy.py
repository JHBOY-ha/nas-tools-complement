# -*- coding: utf-8 -*-

from __future__ import unicode_literals

from unittest import TestCase

import anitopy
from tests.fixtures.table import table, failing_table


class TestAnitopy(TestCase):
    def test_episode_with_completion_marker(self):
        # Completion metadata may share the episode's bracket without hiding it.
        for marker in ('END', 'end', 'FINAL'):
            for number in ('1', '12', '28', '028', '150'):
                for block in ('[%s %s]', '(%s %s)', '[%s_%s]', '[%s%s]'):
                    filename = '[DMG][BOCCHI_THE_ROCK!]%s[1080P][GB].mp4' % (block % (number, marker))
                    with self.subTest(filename=filename):
                        elements = anitopy.parse(filename)
                        self.assertEqual(number, elements.get('episode_number'))
                        self.assertEqual('BOCCHI THE ROCK!', elements.get('anime_title'))
                        self.assertEqual(marker, elements.get('release_information'))

    def test_completion_marker_does_not_relax_other_number_rules(self):
        # Reject arbitrary annotations, title fragments, years and resolutions.
        for block in ('[12 EXTRA]', '[12 END extra]', '[extra 12 END]',
                      '[2022 END]', '[1080P END]', '[2022END]', '[28ENDING]',
                      '[28END extra]', '[extra 28END]', '[12][END]'):
            with self.subTest(block=block):
                elements = anitopy.parse('[DMG][BOCCHI_THE_ROCK!]%s[GB].mp4' % block)
                self.assertEqual('12' if block == '[12][END]' else None,
                                 elements.get('episode_number'))

    def test_completion_episode_respects_parse_option(self):
        for block in ('[12 END]', '[28END]'):
            elements = anitopy.parse('[DMG][BOCCHI_THE_ROCK!]%s[1080P][GB].mp4' % block,
                                     options={'parse_episode_number': False})
            self.assertNotIn('episode_number', elements)

    def parse_options(self, entry_options):
        if entry_options is None:
            return {}

        options = {}
        for option, value in entry_options.items():
            option_name = option.split('option_')[1]
            options[option_name] = value
        return options

    def test_table(self):
        for index, entry in enumerate(table):
            filename = entry[0]
            options = self.parse_options(entry[1])

            elements = anitopy.parse(filename, options=options)

            expected = dict(entry[2])
            if 'id' in expected.keys():
                del expected['id']
            self.assertEqual(expected, elements, 'on entry number %d' % index)

    def test_fails(self):
        failed = 0
        working_tests = []
        for index, entry in enumerate(failing_table):
            filename = entry[0]
            options = self.parse_options(entry[1])

            try:
                print('Index %d "%s"' % (index, filename))
            except:  # noqa: E722
                print(('Index %d "%s"' % (index, filename)).encode("utf-8"))

            elements = anitopy.parse(filename, options=options)

            expected = dict(entry[2])
            if 'id' in expected.keys():
                del expected['id']
            try:
                self.assertEqual(expected, elements)
                working_tests.append(index)
            except AssertionError as err:
                failed += 1
                print(err)
                print('----------------------------------------------------------------------')  # noqa E501

        print('\nFailed %d of %d failing cases tests' % (
            failed, len(failing_table)))
        if working_tests:
            print('There are {} working tests from the failing cases: {}'
                  .format(len(working_tests), working_tests))
