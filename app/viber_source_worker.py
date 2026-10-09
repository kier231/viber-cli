"""Isolated Qt SQL reader. JSON lines in/out; no desktop controls or sending."""

import json
import os
from pathlib import Path
import sys

from app.viber_database import DatabaseReadError, read_snapshot, source_identity, validate_schema
from app.viber_memory import discover_keys, KeyDiscoveryError


class QtViberSource:
    def __init__(self):
        if sys.platform != 'win32':
            raise DatabaseReadError('The live Viber database reader requires Windows.')
        base = Path(os.environ.get('VIBER_CLI_VIBER_DIR', str(Path(os.environ['LOCALAPPDATA']) / 'Viber'))).resolve()
        profile = os.environ.get('VIBER_CLI_PROFILE')
        paths = ([Path(profile).resolve() / 'viber.db'] if profile else
                 list((Path(os.environ['APPDATA']) / 'ViberPC').glob('*/viber.db')))
        if len(paths) != 1 or not paths[0].is_file():
            raise DatabaseReadError('Select one Viber profile using VIBER_CLI_PROFILE, then restart the app.')
        self.path = paths[0]
        self.identity = source_identity(self.path)
        try:
            from PySide6.QtCore import QCoreApplication, QPluginLoader, qVersion
            from PySide6.QtSql import QSqlDatabase, QSqlQuery
        except ImportError:
            raise DatabaseReadError('Install requirements.txt to enable the Viber database reader.') from None
        self.app = QCoreApplication([])
        plugin = base / 'plugins' / 'sqldrivers' / 'qsqlite.dll'
        metadata = QPluginLoader(str(plugin)).metaData()
        version = metadata.get('version', 0)
        expected = f'{version >> 16}.{(version >> 8) & 255}.{version & 255}'
        # Plugin metadata encodes the ABI series (patch may be zero even when
        # Viber's Qt DLL is patched). Require the same major/minor series.
        if expected.split('.')[:2] != qVersion().split('.')[:2]:
            raise DatabaseReadError(f'Viber uses Qt {expected}; install matching PySide6-Essentials before reading.')
        QCoreApplication.setLibraryPaths([str(base / 'plugins')])
        self.QSqlQuery, self.QSqlDatabase = QSqlQuery, QSqlDatabase
        self.db = None
        keys = discover_keys(base / 'Viber.exe')
        for key in keys:
            connection = QSqlDatabase.addDatabase('QSQLITE', 'viber-reader')
            connection.setDatabaseName(str(self.path))
            connection.setConnectOptions('QSQLITE_OPEN_READONLY;QSQLITE_BUSY_TIMEOUT=1000')
            if connection.open():
                query = QSqlQuery(connection)
                query.exec("PRAGMA hexkey='" + key + "'")
                if query.exec('SELECT count(*) FROM sqlite_master') and query.next():
                    query.finish()
                    del query
                    self.db = connection
                    break
                del query
            connection.close()
            del connection
            QSqlDatabase.removeDatabase('viber-reader')
        del keys
        if self.db is None:
            raise DatabaseReadError('Viber database key validation failed. Restart Viber and retry.')
        self.query('PRAGMA query_only=ON')
        validate_schema(self.query)
        self.last_version = None

    def query(self, sql, parameters=()):
        query = self.QSqlQuery(self.db)
        if parameters:
            ok = query.prepare(sql)
            for parameter in parameters:
                query.addBindValue(parameter)
            ok = ok and query.exec()
        else:
            ok = query.exec(sql)
        if not ok:
            # Never expose SQL/errors that could contain keys or private text.
            raise DatabaseReadError('Viber database read failed; detection is paused and will retry.')
        record = query.record()
        columns = [record.fieldName(i) for i in range(record.count())]
        rows = []
        while query.next():
            rows.append({name: query.value(i) for i, name in enumerate(columns)})
        query.finish()
        return rows

    def read(self, request):
        if source_identity(self.path) != self.identity:
            raise DatabaseReadError('Viber profile was replaced. Restart the reader to create a new baseline.')
        version = (self.query('PRAGMA data_version')[0]['data_version'], tuple(sorted(request['phones'])))
        if version == self.last_version:
            return {'source_id': self.identity, 'unchanged': True}
        self.query('BEGIN')
        try:
            result = read_snapshot(self.query, source_id=self.identity,
                                   own_phone=self.path.parent.name, phones=request['phones'],
                                   after_event_id=int(request.get('checkpoints', {}).get(self.identity, 0)))
            self.query('COMMIT')
            self.last_version = version
            return result
        except Exception:
            self.query('ROLLBACK')
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
            self.QSqlDatabase.removeDatabase('viber-reader')


def main():
    source = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get('action') == 'close':
                break
            if source is None:
                source = QtViberSource()
            result = source.read(request)
            response = {'ok': True, 'result': result}
        except (DatabaseReadError, KeyDiscoveryError) as exc:
            response = {'ok': False, 'error': str(exc)}
        except Exception:
            response = {'ok': False, 'error': 'Viber database reader failed. Detection is paused.'}
        print(json.dumps(response, ensure_ascii=True), flush=True)
    if source:
        source.close()


if __name__ == '__main__':
    main()
