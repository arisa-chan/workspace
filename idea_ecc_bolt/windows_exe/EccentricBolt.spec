# -*- mode: python ;-*-
# PyInstaller spec for a single-file Windows executable of EccentricBolt.
# Only the Qt modules actually used are bundled (Widgets/Gui/Core + Agg
# matplotlib backend); QtWebEngine/Qml/Quick etc. are excluded to keep the
# executable small.
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['../main.py'],
    pathex=['..'],
    binaries=[],
    datas=[('../eccbolt', 'eccbolt'), ('../docs', 'docs')] +
          collect_data_files('matplotlib', excludes=['**/tests/**']),
    hiddenimports=['eccbolt', 'eccbolt.gui', 'eccbolt.icr', 'eccbolt.iom',
                   'eccbolt.model', 'eccbolt.analysis', 'eccbolt.presets',
                   'eccbolt.validation', 'eccbolt.mock_service',
                   'eccbolt.idea_service', 'eccbolt.view2d', 'eccbolt.view3d',
                   'eccbolt.ck_table', 'eccbolt.benchmarks',
                   'eccbolt.calibration', 'eccbolt.fe_fallback',
                   'scipy.spatial', 'openseespy', 'openseespy.opensees',
                   'openseespywin'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtWebEngine', 'PySide6.QtWebEngineCore',
              'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets',
              'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtCharts',
              'PySide6.QtDataVisualization', 'PySide6.QtMultimedia',
              'PySide6.QtNetwork', 'PySide6.QtBluetooth', 'PySide6.QtSql',
              'PySide6.QtTest', 'PySide6.QtXml',
              'tkinter', 'pytest'],
    # pyparsing.testing (pulled in by matplotlib) needs unittest at import time
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
          name='EccentricBolt', debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, compress=True, runtime_tmpdir=None,
          console=True, icon=None)
