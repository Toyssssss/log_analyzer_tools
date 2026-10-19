# Cluster Log Analyzer

递归解压多层嵌套的日志压缩包，并在解压结果中做关键词检索。面向
HarmonyOS/Android hilog 一类的设备日志包设计。

典型输入：

```
Log_CLUSTER_20260901194130.zip          (229MB, 90 个顶层条目)
└─ CLUSTER/cluster/<name>.zip           (每个含 8~10 个嵌套 zip)
   └─ Eventid_..._rda_running.zip
      └─ hilog/*.log                    (最终日志)
```

实测该包展开后为 **776 个压缩包 / 6,674 个日志文件 / 2.1GB**，全量解压约 17 秒。

## 安装与运行

运行时**零第三方依赖**，只需 Python 3.9+（标准库 tkinter 提供图形界面）。

```bash
python -m log_analyzer                 # 图形界面
python -m log_analyzer --help          # 命令行
```

## 图形界面

只有一个入口 **Open…**，它先问你要打开哪一类，再弹出对应的原生对话框
（tkinter 的原生对话框无法在同一个窗口里既选文件又选目录）。两条路径的
检索语义完全一致，区别只在字节从哪来：

1. **Open… ▸ An archive or a single file…** 选择压缩包。程序会先遍历目录
   结构（不实际解压），弹出提示告诉你将展开多大、需要多少磁盘。
2. 解压完成后输入关键词 → **Search**。
3. 结果列表每个文件一行，含压缩包内的路径与命中数。
   **点击展开**显示该文件的所有命中行；**收起**则只留文件名。
   选中一行，下方显示完整路径与命中内容，可复制。

### 就地检索：打开文件夹与单个文件

- **Open… ▸ A folder…** 选一个目录，**递归检索整棵树**：该目录下的所有
  文件、所有子目录、以及子目录的子目录里的所有文件。散落的日志文件
  **就地读取，不复制**，也不占额外磁盘。状态栏会标注
  `(searched in place)`。符号链接/junction 环会被剪掉，读不到的子目录
  只记为警告，不会中断整个扫描。
- **文件夹里的压缩包会被解压后再检索**，而不是把压缩包本身当成一个
  二进制文件去扫它的压缩字节。也就是说，`logs/sub/bundle.zip` 里的
  `inside/d.log` 会**以 `sub/bundle.zip/inside/d.log` 这一条结果出现**，
  内容也真的被搜到。这些压缩包按内容身份存进工作区（key 前缀为
  `local-`），同一目录再次打开时直接命中缓存，速度很快；文件被改动过
  则自动重新解压，不会给出过期结果。状态栏会提示「N archive(s) in the
  folder extracted」，`Clear current archive…` 会把它们删掉（散落文件不受
  影响）。
- **Open…** 里选一个**普通文件**（不是压缩包）也一样按单文件就地检索。
- 改名或删除文件后直接点 **Search**，会提示目录已不存在，不会静默给空结果。

### 下方详情面板

选中结果行后，面板会把该文件**整个读出来**（而不是只用列表里的命中行），
所以能直接看到命中行前后的上下文。

- **Matched lines only**（默认）—— 只显示该文件的命中行。这块用的是检索
  自己的命中列表，因此和上方结果行的计数永远一致。命中列表在单文件
  第 500 条处截断，此时会明确标注 `(first 500 listed)`，不会假装列全了
- **All file text** —— 显示整个文件，命中行仍然高亮
- **Filter in file** —— 在文件里挑行。空格或逗号分隔多个词，
  多个词之间是 **OR**（任意一个命中即保留）。它只会往视图里**加**行，
  不会减：matched-only 模式下显示的是 命中行 **∪** 过滤命中的行，
  所以在文件里搜一个不在命中列表里的词，是把它连同上下文一起带出来，
  而不是把视图筛空；两个模式都适用，All file text 本来就显示全部，
  过滤在那里只负责高亮。进程号/线程号不做特殊解析：
  实测真实语料只有 19.1% 的行是 hilog 格式，其余（kmsg、el2、JSON）没有
  pid 列可解析，所以一切按字面文本匹配
- **↑ / ↓** —— 跳到上一个/下一个高亮行，该行整行以**浅黄色**高亮。
  跳转目标是**当前屏幕上带绿色高亮的所有行**：既包括检索关键词的命中，
  也包括 **Filter in file** 里过滤词的命中 —— 两者都是绿色高亮，
  也都是你在找的东西，所以都能一步跳到。走到头会停在第一个/最后一个
  并给出提示，不会绕回。

详情面板的筛选**跨文件保留**——顺着一个 pid 连看几个文件是常见用法，
输入框里还留着该词。

检索选项：

- **Keyword** —— 空格、逗号或分号分隔多个关键词，多词之间是 **OR**
  （任意一个命中即算命中），与 **Filter in file** 同一套规则。
  只填分隔符（如 `,,,`）会提示重新输入，而不是变成「全部命中」
- **Match case** —— 不勾选（默认）忽略大小写
- **Whole word** —— 要求匹配两侧不是字母、数字或下划线；
  多个关键词时**逐个词**各自要求整词
- **Hide binary** —— 隐藏含 NUL 字节的文件（默认**不**隐藏）

工作区在 `Workspace` 菜单里可查看占用、按包清理或全部清空。

### 解压目录（Cached）可手动指定

工具栏的 **Change…** 按钮（或 `Workspace ▸ Change workspace…`）可以换一个
解压目的地，选完即生效并**写入配置文件**，下次启动沿用：

- Windows：`%LOCALAPPDATA%\LogAnalyzer\settings.json`
- 其他平台：`$XDG_CONFIG_HOME/log-analyzer/settings.json`

工具栏标签会显示当前路径与**来源**（`(saved)` 表示来自配置文件，
`(LOG_ANALYZER_WORKSPACE)` 表示被环境变量覆盖）。
作业运行期间不允许切换——正在跑的 worker 会按旧路径写盘。
若配置文件因权限/杀软占用而写不进去，只会在状态栏提示，切换本身照常生效。

## 命令行

```bash
# 只看结构，不解压
python -m log_analyzer scan Log_CLUSTER_20260901194130.zip

# 递归解压到工作区（已解压过则直接复用）
python -m log_analyzer extract Log_CLUSTER_20260901194130.zip

# 检索
python -m log_analyzer search Log_CLUSTER_20260901194130.zip -k sub_temp --lines
python -m log_analyzer search Log_CLUSTER_20260901194130.zip -k sub_temp \
    --whole-word --match-case --json

# 工作区管理
python -m log_analyzer cache list
python -m log_analyzer cache clear
python -m log_analyzer cache clear Log_CLUSTER_20260901194130.zip
```

退出码：找到命中返回 0，未找到返回 1，参数或数据错误返回 2。

## 工作区

解压结果按压缩包身份（路径 + 大小 + mtime 的 SHA1 前 16 位）缓存，
因此**同一个包再次检索是秒级的**：首次全量检索 2.1GB 约 16 秒，
第二次直接从缓存读取。

位置解析顺序：

1. `--workspace` 参数
2. `LOG_ANALYZER_WORKSPACE` 环境变量
3. settings.json 里保存的选择（GUI 的 **Change…** 按钮写入）
4. Windows：`%LOCALAPPDATA%\LogAnalyzer\workspace`
5. 其他：`$XDG_CACHE_HOME/log-analyzer` 或 `~/.cache/log-analyzer`
   （目录名是历史遗留、故意没跟着产品改名：改了会让已在用的工作区失效、
   需要重新解压）

环境变量**优先于**配置文件是有意的：它是企业/配额受限环境下的正式覆盖
手段，一个陈旧的配置文件不该悄悄压过它。GUI 会在标签上标出来源，
所以「Change… 好像没记住」这种情况是可见的。

企业环境里 AppData 常受配额限制，此时用前两种方式改到别处：

```bash
python -m log_analyzer --workspace /data/log-ws extract big.zip
```

**磁盘占用要有心理准备**：单个日志包展开后是 2.1GB 量级。
不搜了就 `cache clear`。

### 中断与恢复

解压按**顶层条目**推进，每完成一个就追加一行记录。中断后重开同一压缩包会
提示「已完成 N 部分，继续还是重来」。恢复只会补齐缺失的顶层条目，不会重复解压。

未完成的解压**不会被当作完整的**：界面会明确标注 partial，检索前会再确认一次，
避免把不完整的覆盖范围误当成全部。

## 检索语义

- 关键词是**字面量**，不是正则 —— `.` 和 `*` 没有特殊含义
- **Whole word 不使用 `\b`**。`\b` 基于 ASCII 词字符，遇到 CJK 相邻会误判：
  `msg: 温度sub_temp` 在 `\b` 规则下**不会**算作边界。这里改用显式断言，
  中英文行为一致
- 匹配逐行进行，跨行匹配不可能
- 行首空白**保留**：hilog 的前导空格是字段对齐，去掉会破坏时间戳/pid/tag 的可读列结构

### 编码处理

日志正文按**字节**匹配，编码只影响显示，因此混合编码或部分损坏的文件不会导致
异常或静默错配。显示时先试 UTF-8，不通过则按 GBK 解码（`errors='replace'`）。

压缩包内的**文件名**编码单独处理：部分 Windows 压缩工具会写入 GBK 字节却不置
UTF-8 标志位，`zipfile` 会把它们按 cp437 解释成乱码。程序会还原原始字节并重试
UTF-8/GBK/Big5，因此中文文件名在两个平台上都能正确显示。

**已知局限**：ASCII 关键词走字节匹配路径，其忽略大小写遵循 ASCII 规则，
所以查 `cafe` 不会匹配 `CAFÉ`。含非 ASCII 的关键词则走 Unicode 路径，
有完整的大小写折叠。这样取舍是为了保证 GBK 日志的解码正确性。

## 安全边界

解压有硬性上限：深度 8 层、30 万条目、总 20GB、单文件 2GB、压缩比 1000:1。

磁盘上的文件名**完全不来源于压缩包内的成员名**（使用 `data/<分片>/<序号>` 短路径），
所以 `../../etc/passwd` 这类条目在结构上就不可能写到工作区之外，
无需依赖名字清洗。文件名超长也不会触发 Windows 的 255 字符单段限制 ——
这正是早期实现会在真实数据上直接失败的原因。

## 打包成独立可执行文件

PyInstaller **不能交叉编译**：在 Windows 上构建出 Windows 版本，在 Linux 上构建出
Linux 版本。本仓库的 GitHub Actions 会同时产出两者。

```bash
python -m pip install -r requirements-build.txt
packaging/build.bat          # Windows
./packaging/build.sh         # Linux
```

产物在 `dist/`：

- `Cluster-log-analyzer/` —— 图形界面（onedir，无控制台窗口）
- `Cluster-log-analyzer-cli/` —— 命令行

**用 onedir 而不是 onefile**：onefile 每次启动都要把约 15MB 的 Tcl/Tk 解到临时目录
（tkinter 下实测 1~3 秒启动开销），而且自解压单文件是 Windows Defender 与企业
管理软件的典型误报触发源。分发时**整个文件夹一起给**，不要只拷 exe。

Linux 构建要求系统装有 tkinter：

```bash
sudo apt-get install -y python3-tk      # Debian/Ubuntu
sudo dnf install -y python3-tkinter     # RHEL/Fedora
```

## 开发

```bash
python -m unittest discover -s tests -t . -v
```

测试不依赖那个 229MB 的真实数据包，全部用程序化构造的合成压缩包：
多层嵌套、GBK 文件名（手工构造、不置 UTF-8 标志位）、GBK 正文、
高压缩比炸弹、`../../evil` 这类路径穿越名、tar.gz 分支、
中断后的 JSONL 恢复、以及撕裂的 JSONL 末行。
图形界面也通过真实 widget + 事件循环泵做端到端测试。
