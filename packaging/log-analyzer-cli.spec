a = Analysis(
    ['_entry_cli.py'],
    pathex=['..'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=['numpy', 'pandas', 'matplotlib', 'PIL', 'pytest', 'unittest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Cluster-log-analyzer-cli',
    console=True,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name='Cluster-log-analyzer-cli')
