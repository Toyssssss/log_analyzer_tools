a = Analysis(
    ['_entry_gui.py'],
    pathex=['..'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=['numpy', 'pandas', 'matplotlib', 'PIL', 'pytest', 'unittest',
              'email', 'http', 'xml', 'pydoc_data'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Cluster-log-analyzer',
    console=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name='Cluster-log-analyzer')
