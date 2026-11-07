# files

文件与 IPA 存放仓库，通过 GitHub Release 提供直链下载，并自动维护一个 [AltStore](https://altstore.io) 源。

## AltStore 源

```
https://github.com/worldhellotravel1999-hue/files/releases/download/nightly/altstore.json
```

在 AltStore / SideStore 里打开 **Sources → +**，粘贴上面的地址即可添加。

当前收录（最新列表以源文件为准）：

| App | 版本 | Bundle ID |
|---|---|---|
| iCTRL | 1.0.0 | `com.openminis.ictrl` |
| iCTRLUITests-Runner | 1.0.0 | `io.worldhellotravel1999.ictrluitests-runner` |
| StikDebug | 3.1.15 | `com.stik.stikdebug` |
| StikPair | 1.2.0 | `com.stik.StikPair` |
| 红果短剧 | 7.3.9 | `com.phoenix.video` |

## 添加 / 更新 App

把 `.ipa` 上传到 [`nightly` release](https://github.com/worldhellotravel1999-hue/files/releases/tag/nightly) 即可，无需改代码。

上传完成后到 **Actions → Sync AltStore source → Run workflow** 手动触发一次同步（原因见下面的「触发方式」）。同步会：

1. 读取 IPA 的 bundle id、版本号、最低 iOS 版本；
2. 计算文件大小与 sha256；
3. 从 `Payload/*.app` 提取图标（自动转换 Apple 的 CgBI PNG），提交到 `icons/`；
4. 生成或更新 `altstore.json` 里的 app 与 version 条目，再替换 release 里的源文件。

**文件名建议**（重要）：

- 用 ASCII 命名，例如 `StikDebug-3.2.0.ipa`。GitHub 会把中文文件名替换成下划线，`红果短剧_7.3.9.ipa` 会变成 `_7.3.9_.ipa`。
- 把版本号写进文件名，删除和替换时更容易识别。
- 同一个 App 的多个版本可以并存，文件名不要重复。

## 删除 App / 版本

直接在 release 里删除对应的 `.ipa`，然后运行一次同步：

- 删掉的是某个版本 → 只移除该版本，App 与其他版本保留；
- 某个 App 的最后一个 IPA 被删除 → 整个 App 条目移除；
- 源文件发布成功后，`icons/` 里没有任何源再引用的 `icon-*.png` 会被清理；
- 其他 release 的源仍在引用同一图标时，该图标会保留；
- 手写的图标文件、外部链接的图标不受影响。

## 图标

图标统一放在仓库的 [`icons/`](icons) 目录，release 里只有 `.ipa` 和 `altstore.json`：

```
https://raw.githubusercontent.com/worldhellotravel1999-hue/files/main/icons/icon-<bundleId>.png
```

命名固定为 `icon-<bundleId>.png`。对应的 IPA 没有变化时，同步不会改动已有图标，可以直接替换同名文件；一旦该 App 的 IPA 更新，图标会按新包内的图标重新生成。想长期固定自己的图标，请在 `altstore-meta.json` 里把该 App 的 `iconURL` 指向别的地址。

## 触发方式

| 方式 | 说明 |
|---|---|
| 手动（推荐） | Actions → **Sync AltStore source** → Run workflow。默认同步 `nightly`，也可填其他 tag |
| 定时 | 每 10 分钟检查一次。GitHub 的调度可能延迟数分钟甚至跳过，不保证时效 |
| Release 事件 | release 被创建 / 发布 / 编辑时触发。**单独上传或删除附件不触发此事件** |
| 本地 | 见下方「本地运行与测试」 |

因此「上传完就自动更新」并不保证立刻发生；批量上传或删除后，手动点一次 Run workflow 最稳妥。

## 匹配规则

同一个 App 的判定顺序：

1. 文件名与该 App 已有条目的 `downloadURL` 完全一致 → 视为同一个 App（版本号沿用原条目，只刷新大小、sha256、日期）；
2. 否则取文件名开头与 App 名称的**最长**前缀匹配（`StikDebug-3.2.0.ipa` → `StikDebug`）；
3. 都不匹配时按 IPA 里的 `CFBundleIdentifier` 合并；仍找不到才新建 App。

> 重签名工具常会改写 `CFBundleIdentifier`（例如都变成 `com.nuownqyyy691.ct12566`），所以第 1、2 条优先于 bundle id。中文 App 名按 Unicode 字母数字匹配，不会因为去除非 ASCII 字符而被误判。

同名 IPA 被替换且内部版本号变化时，旧版本条目会被移除——因为该下载链接现在指向的是新文件，保留会造成「旧版本装到新包」。

## 元数据覆盖（可选）

在 [`.github/altstore-meta.json`](.github/altstore-meta.json) 里按 bundle id 覆盖字段，脚本不会覆盖已存在的 App 元数据：

```json
{
  "com.example.demo": {
    "developerName": "Example Studio",
    "category": "utilities",
    "localizedDescription": "一句话介绍这个 App。"
  }
}
```

支持 `name`、`developerName`、`localizedDescription`、`category`、`subtitle`、`tintColor`、`iconURL`。以 `_` 开头的键会被忽略。

新建 App 未配置时的默认值：`developerName` 为 `Travel`，`category` 为 `utilities`。

## 缓存与状态

`.github/altstore-state/<release_id>.json` 记录每个 IPA 的资产 ID、大小、更新时间与摘要；未变化的 IPA 会跳过下载，避免每次同步重复拉取大文件。该文件由工作流自动提交，不需要手动维护，删除后下次同步会重建。

## 本地运行与测试

```bash
# 预演，不写任何东西
GITHUB_TOKEN=... GITHUB_REPOSITORY=worldhellotravel1999-hue/files TAG=nightly DRY_RUN=1 \
  python3 .github/scripts/sync-altstore.py

# 实际执行
GITHUB_TOKEN=... GITHUB_REPOSITORY=worldhellotravel1999-hue/files TAG=nightly \
  python3 .github/scripts/sync-altstore.py

# 回归测试（工作流每次运行前也会执行）
python3 -m unittest discover -s .github/scripts -p 'test_*.py'
```

## 文件结构

```
.github/
  workflows/sync-altstore.yml   # 定时 / 手动触发
  scripts/sync-altstore.py      # 同步主逻辑（纯标准库）
  scripts/reconcile.py          # 删除对账、图标回收
  scripts/test_sync.py          # 15 项回归测试
  altstore-meta.json            # 可选元数据覆盖
  altstore-state/               # 资产快照缓存（自动生成）
icons/                          # App 图标（自动生成/清理）
```

## 已知限制

- 上传或删除附件不会即时触发工作流，只能等定时任务或手动运行。
- 定时任务由 GitHub 调度，可能延迟或跳过；仓库连续 60 天无提交时 GitHub 会自动停用定时工作流。
- 同步过程中如果 release 正在上传、资源列表读取失败或列表在运行期间发生变化，本轮会中止并等待下次重跑，不会基于不完整的列表做删除。
- 删除图标属于不可逆操作，脚本只在确认全部 release 源都不再引用后才执行。
