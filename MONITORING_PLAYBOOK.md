# 产品库长期运行手册

本文档是游戏公司产品库的持久化工作约定。聊天上下文、自动摘要或临时报告与本文档冲突时，应先停下来核对代码和证据，不能凭摘要覆盖这里的规则。

## 每日完整更新

1. 从 `data/*.js` 加载全库，先检查重复 `platform:pkg_or_id`。
2. 使用 Apple US Lookup 扫描当前 iOS 开发者账号并更新评分、版本日期、预上架状态及开发者迁移。
3. 扫描 Google Play 当前开发者账号并更新新应用、下载量、评分数和开发者迁移。
4. 开发者主页失败时，逐个访问该开发者关联的所有历史应用，包括已标下架记录，不得抽样；历史状态不能代替当前核验。
5. 保存本轮 GP 扫描错误总结，并把同一轮总结传给国内 AppMagic 对账，不能引用旧日期缓存文件。
6. 运行 AppMagic 自动修复门禁。
7. 门禁通过后才允许提交和推送；门禁未通过时保留本地更新并明确报告原因。

## AppMagic 自动修复门禁

自动导入必须同时满足：

- 公司不属于默认排除的海外范围；
- AppMagic united publisher 只关联一个库内公司；
- 至少一个已确认库内应用锚定该 publisher；
- 候选当前原生商店页在线；
- 候选未在其他公司中出现；
- AppMagic 映射和 publisher 库存抓取完整。

导入步骤：

1. 从原生商店重新获取应用名称、图标、开发者名称、开发者链接、评分、下载量、上架日期和更新时间。
2. Google Play 上架日期按 `Released on -> AppMagic Release Date -> AppMagic First Detected -> APKCombo 完整日期 -> Google Play Updated on` 回退。
3. iOS 只用美国区 Lookup 更新评分数；未来日期按预上架规则处理。
4. AppMagic 已明确把原生开发者账号归入该 united publisher 时，直接展开该 Apple/Google 账号并递归处理当前可访问的新应用，不对账号下每款应用重复查询同一个归属结论。
5. 再次执行 AppMagic 全量完整性对账，检查是否还有未处理漏项以及数据是否收敛。它不是对刚导入应用重复做归属确认。默认最多修复三轮。

只在下列情况下阻断：

- AppMagic 或当前商店核验存在网络错误、限流、缓存未刷新或返回不完整；
- publisher 关联多个公司；
- 候选已属于另一家公司；
- 在线候选的完整商店字段无法取得；
- 当前开发者页明确列出了新应用，但该应用详情无法导入；
- 自动修复没有进展；
- 达到修复轮数上限后仍存在在线漏项。

- 每个 AppMagic 库外候选必须写入当次 `appmagic_missing_candidates.csv` 和主报告，至少包含公司、平台、ID、原生开发者、publisher、证据等级和未导入原因；不能只报告候选总数。
- 候选状态必须明确区分 `strong_*`（可自动修复）、`*_collision`/`*_shared_*`（跨公司冲突）和 `manual_review_insufficient_evidence`（没有已确认库内锚点或其他强证据）。禁止留下空状态或笼统的“证据不足”。

旧开发者主页无法展开本身不是阻断理由。它只能在 AppMagic 库存完整、所有已知当前应用页核验完成且最终漏项为零时降为警告。

## 开发者改名、迁移和拆分

- 不以开发者显示名称作为唯一身份，优先使用原生开发者 ID、开发者链接、应用 ID 和 AppMagic publisher。
- Apple 结果先按全库 `iOS:trackId` 去重；同公司更新当前 `artistName` 和 `artistId`，跨公司只记录冲突。
- GP 旧账号失效后，从每个在线应用详情页恢复当前开发者名称和链接。同一旧账号可能拆成多个当前账号，必须分别展开。
- 网络失败不能改写 `removed`。只有明确 404 或商店软 404 才能标记不可用。

## GP 旧入口专项复核

固定实现：`gp_store_verification.py` 提供原生商店状态分类和分页扫描；`review_failed_gp_entries.py` 重放指定监控总结中的失败入口，不硬编码日期或公司清单。

```bash
python3 review_failed_gp_entries.py \
  --summary <指定轮次的监控总结.json> \
  --output-dir ../product_library_private_evidence/<本轮目录> \
  --workers 4
```

- 默认只读。`--resume` 仅复用本轮目录中六小时内的明确应用页结果，失败结果重试，当前账号重新扫描。旧总结重放必须明确标注来源，不能冒充当天完整更新。
- `--apply-identities` 只更新逐应用确认的原生开发者名称/账号链接；已有完整上架日期的在线应用可清除旧 `removed` 标记。缺日期时调用现有完整 GP 详情与日期回退链，成功后恢复，失败保留原状态并报告。保留原公司，不自动导入线索、不写入新的下架标记；库内身份在核验后变化或证据过期时跳过并报告。
- 单独记录旧入口扫描结果、历史应用数、在线数、美国区明确不可访问数、核验错误数、恢复账号及分页是否完整。按名称扩大的匹配只是复核范围，不能当作整账号迁移证据。
- HTTP 200 缺少应用/开发者元数据记为 `incomplete`；只有应用页中明确的不存在提示才算软 404。同意页、验证页、限流页、重定向、格式变化都不能当作下架。
- HTTP 404 仅表示当次地区请求不可访问，不直接宣称全球下架；专项脚本不据此改写产品状态。
- 普通 GP 扫描必须解析开发者库存的 `ds:3` 和后续 `qnKhOb` 分页；仅从该账号库存提取包名，不能抓整页推荐链接。未知结构、重复 token、轮数上限必须保留为不完整。
- `qnKhOb` 的旧库存外层也可能包含新版卡片（包名位于 `[12,0]`）；外层和卡片布局分别解析，不能因混合布局丢失后续页。
- 原生账号 ID 不随显示名称改写。名称改变、应用转移、账号拆分都从应用详情恢复；同一账号下新包仍需既定归属和跨公司检查，不能因某款历史应用已转入该账号就把整个账号归给旧公司。
- `gp_candidate_ownership.py` 在新包详情导入前，用入库前已知应用的 AppMagic 归属核对候选 publisher。迁入应用的旧 publisher 不能授权新账号的其他 publisher；AppMagic 尚未收录的新包仅在已有锚点同时确认该原生账号时扩展。无法确认的线索写入 `gp_scan.ownership_pending`，不自动导入。
- 对已启动的旧版本监控，可在结束后用 `review_daily_gp_candidates.py --baseline <本轮开始前的全库备份.json> --output ../product_library_private_evidence/<本轮>/ownership_review.json --apply` 隔离未经授权的新 GP 候选；只移出本轮新增且未获授权的记录，原有应用不改归属，证据保留。修正后重新执行当次门禁和全部发布校验。
- 国内按相关 AppMagic publisher 刷新对账；海外仍核验已知应用和恢复的原生账号，但默认不深度扩展 AppMagic。海外候选和跨公司冲突单列，不自动归属。
- 已成功的应用更新与未确认项目分开记录。部分在线不能掩盖其他核验失败；门禁统计必须包含 `incomplete` 和未检查应用，不能虚报完成。本专项不改变完整更新的提交/推送授权，也不绕过既定发布校验。
- GP 旧开发者页复核报告必须保存逐应用状态和错误详情；`404/soft_404`、`incomplete`、超时/限流/连接错误和未检查不能合并成一个“无法访问”数字。
- `app_pages.jsonl`、`current_accounts.json`、`old_accounts.json`、`missing_candidates.json`、`entry_review.csv` 和身份修改记录只留在仓库外私有目录。

## 证据和关联

证据优先级：

1. AppMagic 明确的 united publisher 归属；
2. 当前原生商店开发者账号及应用 ID；
3. 官网、支持页和隐私政策中的商店链接；
4. 图标、`app-ads.txt`、邮箱及其他辅助线索。

官网、支持页和隐私政策中看到的邮箱要写入仓库外的私有证据备份。邮箱域名不参与递归发现。`app-ads.txt` 中 AppLovin、Unity、Mintegral、Google 等常见网络重合不得自动改变公司归属。

## 日期规则

- iOS 未来 `releaseDate` 保存为 `YYYY/MM/DD[预]`，排序只使用日期前缀。
- 预上架应用每天复查。Apple 确认正式上线后，用实际首次上架日期替换并移除 `[预]`。
- 未来 `currentVersionReleaseDate` 不写入 `last_update`。
- Google Play 日期必须来自既定回退链，不得臆造或使用只有年份的日期。

## 提交前校验

Apple 候选核验必须校验 HTTP 状态、JSON 结构、resultCount 和原生元数据。美国区 Lookup 成功且未返回对应 ID 记为 `not_found_us`，单列地区缺席警告，不视为网络失败，不标记全球下架；超时、429、无效或不完整响应仍阻断发布。评分更新仍只使用美国区。

- Python 编译通过。
- `monitor.git_commit_push()` 必须先调用 `validate_before_publish()`；编译、全部测试、diff 和 Node 检查任何一项失败，都不得暂存、提交或推送。不能仅靠操作者记住顺序。
- 全部 `test_*.py` 通过。
- `git diff --check` 通过。
- 全库无重复 `platform:pkg_or_id`。
- 运行固定校验脚本 `node ./validate_library.js`，逐字段比较产品索引和公司文件，检查公司统计、重复 ID 和在线产品日期。
- `index.html`、`product_index.js` 和各公司文件数量一致。
- 当前在线产品上架日期无缺失。
- iOS 仅评分数变化也必须计入更新及受影响公司，写回公司文件后再生成索引；不能只改内存或产品索引。
- 报告本次每个新增应用及所属公司；不能只给总数。
- `outputs/`、私有证据和运行报告不得进入 Git。

## 规则修改

以后任何人改变上述行为，都必须同时修改实现、测试、本文档和 `game-product-library` skill。只改其中一处视为未完成。
