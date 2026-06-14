# Installer for metoffice_datahub
# Copyright 2024 WeeWX Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from weecfg.extension import ExtensionInstaller


def loader():
    return MetOfficeDatahubInstaller()


class MetOfficeDatahubInstaller(ExtensionInstaller):
    def __init__(self):
        super().__init__(
            version="0.1",
            name='metoffice_datahub',
            description='UK Met Office DataHub Site Specific hourly forecast service',
            author="WeeWX Contributors",
            author_email="",
            config={
                'MetOfficeDatahub': {
                    'api_key': 'REPLACE_WITH_YOUR_API_KEY',
                    'latitude': '51.5',
                    'longitude': '-0.12',
                    # 10800 s = 3 hours = 8 calls/day (free plan allows 360/day, min interval 240 s)
                    'fetch_interval': '10800',
                    'max_hours': '72',
                    'data_binding': 'metoffice_binding',
                },
                'DataBindings': {
                    'metoffice_binding': {
                        'database': 'metoffice_sqlite',
                        'manager': 'weewx.manager.Manager',
                        'table_name': 'forecast',
                        'schema': 'user.metoffice_datahub.schema',
                    }
                },
                'Databases': {
                    'metoffice_sqlite': {
                        'database_name': 'archive/metoffice.sdb',
                        'database_type': 'SQLite',
                    }
                },
                'Engine': {
                    'Services': {
                        'data_services': 'user.metoffice_datahub.MetOfficeDatahub',
                    }
                },
            },
            files=[
                ('bin/user', ['bin/user/metoffice_datahub.py']),
            ]
        )
