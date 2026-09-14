# Peewee + MySQL 协议连 openGauss B 兼容模式（个人笔记）

> 用途：开源实习复盘 + 面试闭卷 + RAG 项目增量语料。  
> 规则：**用自己的话写**；写不上再对照 PR / 官方文档补事实。禁止整段复制 AI 生成稿。  
> 状态：`[ ] 未写完` → 全部填完并能脱稿讲 2 分钟后改为 `[x]`


---

## 1. 环境与版本（必须背熟）

| 项 | 你的实际值 |
|----|------------|
| 操作系统 | openEuler 20.03 LTS |
| 数据库 | openGauss 7.0.0-RC3 |
| 兼容模式 | B 兼容 + 编码 UTF8 |
| 插件 / 协议 | Dolphin 5.2，MySQL 协议端口 3308 |
| 客户端 | MySQL Client 8.0.28 / gsql （随 openGauss 7.0.0-RC3） |
| Python | 3.10.0 |
| ORM / 驱动 | Peewee 4.4.0 · PyMySQL 1.2.0 |

**一句话总结环境：**

* openGauss **7.0.0-RC3** + B 兼容 + Dolphin **5.2** + MySQL 协议端口例如 **3308**
* Python **3.10** / Peewee **4.3.0** / PyMySQL **1.2.0**
* openEuler 20.03


## 2. 连接前提（能口述 5 步）

按你实际操作顺序写关键词，不要抄长 SQL：

1. **B 库**：如何确认/创建 B 兼容库？编码？
CREATE DATABASE proto_test_db DBCOMPATIBILITY 'B' ENCODING 'UTF8';
   
2. **用户与 schema**：为什么 schema 要和 PyMySQL 的 `database` 参数同名？
 
PyMySQL 的 `database` 参数在 B 库里对应 schema，不是物理库名。\
所以要 `CREATE SCHEMA <名字> AUTHORIZATION <用户>`，且该名字与连接代码里的 `database=` 一致；\
物理库由 `dolphin.default_database_name` 指向。

3. **MySQL native 密码**：和 gsql 登录密码什么区别？用什么函数设置？

gsql 密码用于原生协议；应用走 MySQL 协议要用 native 密码，\
用 `set_native_password(...)` 设置；PyMySQL 里 `password=` 填的是 native 密码。

4. **协议开关**：`enable_dolphin_proto`、端口、`dolphin.default_database_name` 各控制什么？

|   |   |   |
|---|---|---|
    | 配置                              | 管什么                  | 不配/配错会怎样         |
| `enable_dolphin_proto`          | 是否打开 MySQL 协议能力      | 客户端连不上 MySQL 端口  |
| `dolphin_server_port`           | MySQL 协议监听端口（如 3308） | 端口占用或连错成 gsql 端口 |
| `dolphin.default_database_name` | 协议默认落到哪个**物理 B 库**   | 连上后找不到默认库        |

开关打开协议；端口决定连哪；default\_database\_name 决定默认物理库。\
连 Dolphin 用 MySQL 协议端口，不要误用 gsql 的 5432（按你环境写实际端口）。

5. **转义与认证**：为什么可能要 `standard_conforming_strings`？客户端认证要注意什么？

转义 `standard_conforming_strings`：**\
PyMySQL 默认用**反斜杠**转义字符串。若服务端解析方式和驱动不一致，带 `\`、`'` 的字符串可能写坏或报错。\
文档做法：为连接用户设 `standard_conforming_strings = off`，让服务端和 PyMySQL 一致，**改完要重新连接**。

**客户端认证：**

* 规则里写**真实客户端地址/网段**，不要图省事 `0.0.0.0/0`
* 库名、用户名要和实际一致


> PyMySQL 默认反斜杠转义，故用户级 `standard_conforming_strings=off`，改后重连。\
> 认证用具体客户端网段，生产不要 0.0.0.0/0。
 

---

## 3. 我验证过的四类用例

每类写：**测了什么 · 1 个成功例 · 1 个边界/失败例 · 结论**

### 3.1 连接

- 成功例：
- 使用 Peewee 4.3.0 的 `MySQLDatabase` 配合 PyMySQL 1.2.0，经 Dolphin 的 MySQL 协议端口 `3308` 连接 openGauss B 兼容数据库成功。Peewee 读取到的兼容版本为 `server_version=(8, 0, 28)`，执行 `SELECT VERSION()` 返回 openGauss 7.0.0-RC3 的真实版本信息。
  
- 边界/失败例：
- 连接错误端口时精确抛出 `peewee.OperationalError`；认证测试复用已经验证有效的数据库名、主机、端口和用户名，只替换密码。当前版本实测错误密码返回错误码 `514`，经 PyMySQL/Peewee 映射为 `peewee.InternalError`，消息包含 `failed in auth check`。另外验证了不设置 `PEEWEE_TEST_*` 环境变量、仅使用 `config.ini` 时配置可以正确加载。

- 结论：
- Peewee + PyMySQL 可以通过 Dolphin MySQL 协议连接 openGauss B 库。认证失败能够被可靠识别，但当前返回的 `514/InternalError` 与标准 MySQL 常见的 `1045/OperationalError` 仍有差异，本任务测试对此进行精确记录，没有把任意连接异常当成认证失败。

### 3.2 DDL / DML

- 成功例：
- Peewee 可以创建、查询和删除模型表；单条插入可以取得 `lastrowid`；批量插入 10 条后实际查询行数为 10；SELECT、UPDATE、DELETE、`get_or_create()` 均正常。数据库默认值测试使用显式 `DEFAULT 0`，通过原生 INSERT 省略该列，再重新 SELECT，实际落库值为 0。

- 边界/失败例：  、
- 连续两次执行 `create_tables()`，第二次依赖 `IF NOT EXISTS` 幂等完成；关系模型清理按外键依赖逆序执行，先删除子表和中间表，再删除父表。关系测试连续运行两次均为 `3 passed`，teardown 未发现 `test_` 表残留。

- 结论：  常用 DDL、单条及批量 DML 在 B 模式下可以正常使用。数据库 DEFAULT、重复建表和外键表清理均使用了能够真正验证数据库行为的方式，而不是只检查 Python 模型对象。


（可补充：批量插入为何要用 `.as_rowcount()`？）
批量插入需要 `.as_rowcount()`，原因是 Peewee 的批量 INSERT 默认 `execute()` 返回值可能是最后插入 ID 或驱动相关值，实测曾返回 `1`，不能直接当作插入行数。调用 `.as_rowcount()` 后才明确要求返回受影响行数，同时测试还会查询实际记录数，防止只依赖驱动返回值。


### 3.3 类型映射

| Peewee 字段            | B 库字段（按实测）                                        | 备注                                                   |
| -------------------- | ------------------------------------------------- | ---------------------------------------------------- |
| `AutoField`          | `integer` + `AUTO_INCREMENT`                      | 修复后元数据 `extra=auto_increment`，并通过 `SHOW INDEX` 识别为主键 |
| `CharField`          | `character varying`                               | MySQL 协议类型映射目标为 `varchar(n)`                         |
| `IntegerField`       | `integer`                                         | 整数写入和读取正常                                            |
| `DecimalField(10,2)` | 原始元数据曾为 `number`；Server 修复后的原生视图为 `numeric(10,2)` | 结合 MySQL 协议类型映射后目标为 `decimal/decimal(10,2)`          |
| `DateTimeField`      | `timestamp without time zone`                     | 日期时间秒级往返一致                                           |
| `BooleanField`       | `boolean`                                         | MySQL 协议可映射为 `tinyint(1)`                            |
| `TextField`          | `text`                                            | UTF8 B 库中实测为 `text`                                  |
| `FloatField`         | `real`                                            | 浮点字段建表和读写正常                                          |



**与 MySQL 有差异、需要说明的一点：**

openGauss 内部及原生 `gsql` 视图使用 `character varying`、`numeric`、`timestamp without time zone` 等类型名称，而 MySQL/Peewee 通过 MySQL 协议预期 `varchar`、`decimal(p,s)`、`timestamp` 等名称。因此需要同时依赖：

* Plugin 已合入的类型名称映射；
* Server #9709 对 `atttypmod` 精度的保留；
* Dolphin MySQL 协议层的最终输出。

此外，最初使用 SQL\_ASCII 数据库时，`CharField`/`TextField` 曾被识别为 `varbinary`/`blob`；改用 UTF8 B 兼容数据库后恢复为 `character varying`/`text`。


### 3.4 ORM 增删改查 / 事务

- 成功例：
- 已验证模型插入、批量插入、主键查询、条件查询、更新、删除、`get_or_create()`、JOIN、外键 backref 和多对多关系。事务方面验证了 `atomic()` 正常提交、显式 rollback、savepoint 以及自动提交后的跨连接可见性。

- 边界/失败例：
- 在 `atomic()` 中主动抛出异常后，事务内数据全部回滚；savepoint 内部回滚不会影响外层记录。autocommit 测试使用两个不同的物理连接：自动提交记录可以被观察连接立即读取，而写入连接显式事务中尚未提交的记录对观察连接不可见，事务提交后才可见。

- 重复键：错误码/SQLSTATE？在哪一层捕获？为什么不能裸 `except Exception`？
  
  原始 RC3 实测返回 `20674/HY000`，PyMySQL 和 Peewee 均将其识别为 `OperationalError`。Plugin #2553 的修复目标是：

     - MySQL 客户端：`1062/23000`
     - PyMySQL：`pymysql.err.IntegrityError`
     - Peewee：`peewee.IntegrityError`

测试分别在 PyMySQL 驱动层和 Peewee ORM 层精确捕获，并断言错误码 `1062`。不能使用裸 `except Exception`，因为它会把测试代码自己抛出的 `AssertionError`、连接失败、权限错误或其他无关异常一起吞掉，导致“目标 SQL 没有报错”时测试仍然 PASS。
 
- 结论：  
Peewee 的 CRUD、关系模型和主要事务能力可在 Dolphin MySQL 协议下使用。重复键错误映射代码已提交到 Plugin #2553，最新完整流水和最终 Server + Plugin 联合回归仍待完成。

---

## 4. 兼容问题故事 A：主键 / SHOW INDEX（Plugin）

> 对应：Plugin Issue #1626 · Plugin PR #2553 · 实习任务 #35  
> 链接：[[Plugin#2553]{.underline}](https://gitcode.com/opengauss/Plugin/pull/2553)

**现象（你看到的原始输出）：**
原始 `SHOW INDEX` 返回：

`Key_name = test_conn_t3_pkey`

虽然该索引实际是主键索引，但没有使用 MySQL 约定的固定名称 `PRIMARY`。因此 Peewee 得到：

`primary_key = False`


**Peewee / MySQL 侧为何不认：**

（点名具体 API 或判断逻辑，例如 `get_primary_keys` 认什么？）

Peewee 4.3.0 的 `MySQLDatabase.get_primary_keys()` 执行 `SHOW INDEX FROM table`，然后只保留：

`row[2] == 'PRIMARY'`

再取 `row[4]` 作为主键列名。原始输出为 `test_conn_t3_pkey`，所以过滤结果为空。随后 `get_columns()` 判断列名是否存在于主键集合中，最终得到 `primary_key=False`。

**根因（openGauss 侧）：**

Dolphin 的 `public.index_statistic` 视图直接使用：

`c.relname AS key_name`

openGauss 自动生成的主键索引名通常是 `<表名>_pkey`，与 MySQL 固定返回 `PRIMARY` 的元数据约定不一致。

**方案（改了什么、为什么这么改）：**

通过 `pg_index.indisprimary` 判断真实主键索引：

```
CASE
    WHEN EXISTS (
        SELECT 1
        FROM pg_index pi
        WHERE pi.indexrelid = i.indexrelid
          AND pi.indisprimary
    )
    THEN 'PRIMARY'::name
    ELSE c.relname
END AS key_name
```

只修改主键索引的展示名称；普通索引仍返回真实索引名，避免影响已有 DDL、监控或按索引名查询的代码。

**实现时的关键细节（类型、脚本、回归）：**

* 使用 `'PRIMARY'::name`，因为原来的 `c.relname` 类型是 `name`。如果直接返回隐式 `text`，`CREATE OR REPLACE VIEW` 会报：

```
cannot change data type of view column "key_name" from name to text
```

* 修改了 Plugin 的 `sql_script/base.sql`，用于新安装。
* 修改 `dolphin--5.2--5.3.sql`，用于升级。
* 修改 `dolphin--5.3--5.2.sql`，回滚时恢复原始索引名。
* 同步更新 `expected/test_shows_2.out`，覆盖多种 `SHOW INDEX` 写法以及普通索引不受影响的场景。

**验证方式与结果：**

分层验证结果：

```
主键索引：Key_name = PRIMARY
普通索引：仍返回原索引名
Peewee primary_key = True
```

Plugin #2553 当前 HEAD 为 `40a33424`，已经基于最新 Plugin master 推送；最新完整流水仍在验证中。

**面试 60 秒口述稿：**

> 我发现 openGauss 的主键本身是正确的，但 Dolphin 的 SHOW INDEX 返回了 openGauss 自动生成的 `<表名>_pkey`，而 Peewee 的 MySQL 适配代码只认固定字符串 `PRIMARY`，所以主键列被错误标成了普通列。我的修复没有修改真实索引名，而是在 Dolphin 的 `index_statistic` 元数据视图中，依据 `pg_index.indisprimary` 判断真实主键，只把主键的展示名称改为 `PRIMARY`，普通索引保持原名。实现时还要使用 `'PRIMARY'::name` 保持视图列类型不变，并同步提供安装、升级、回滚脚本和 SHOW INDEX 回归预期。


---

## 5. 兼容问题故事 B：自增 extra 大小写（Server）

> 对应：openGauss-server PR #9709（与 #2553 关联）  
> 链接：____https://gitcode.com/opengauss/openGauss-server/pull/9709____

**现象：**

原始 `information_schema.columns` 返回：

```
extra = AUTO_INCREMENT
```

Peewee 最终得到：

```
identity = False
```

**Peewee 侧判断逻辑：**

Peewee 4.3.0 在 `MySQLDatabase.get_columns()` 中使用：

```
'auto_increment' in (extra or '')
```

这是大小写敏感判断。`AUTO_INCREMENT` 不包含小写的 `auto_increment`，所以 `identity=False`。

**根因：**

openGauss-server 的 `information_schema.columns` 视图在检测到内部默认表达式为 `AUTO_INCREMENT` 后，仍把 `extra` 显示为大写 `AUTO_INCREMENT`，没有遵循 MySQL 元数据常见的小写输出形式。

**方案（只改显示还是改内核？升级/回滚？）：**

只修改 `information_schema.columns.extra` 的展示值：

```
AUTO_INCREMENT → auto_increment
```

没有修改内部自增机制、序列或 `adsrc='AUTO_INCREMENT'` 的存储语义。

同时处理了：

* 新安装数据库的 `information_schema.sql`；
* catalog 版本 `93_153` 的 maindb/otherdb upgrade-post；
* 对应 rollback-post，回滚时恢复大写输出；
* rollback 增加 catalog 依赖和当前视图状态保护，避免旧版本缺少 `pg_get_index_type` 等对象时错误重建新视图；
* DECIMAL 的 `COLUMN_TYPE` 在 B 模式下使用 `a.atttypmod` 保留 `(p,s)`；
* 精度修改限定在 `sql_compatibility='B'`，避免影响 Shark/D 模式等其他兼容模式。

**验证方式与结果：**

运行时替换视图后实测：

```
id     | integer | integer       | PRI | auto_increment
amount | numeric | numeric(10,2) |     |
```

配合 Plugin 类型名映射后，目标 MySQL 协议结果为：

```
data_type  = decimal
column_type = decimal(10,2)
identity = True
```

Server #9709 的 HEAD `e377fb225` 主流水 #32221 已成功，并已由老师确认合入。

**和故事 A 的关系（一句话）：**

两处都是“数据库对象本身正确，但 MySQL 元数据展示不符合 Peewee 判断规则”：#2553 修复 `primary_key=True`，#9709 修复 `identity=True` 并保留 DECIMAL 精度，两者组合后 `get_columns()` 才能得到完整结果。

**面试 60 秒口述稿：**

> 自增功能本身可以正常插入和回读，但 Peewee 仍把 identity 标记为 False。原因是 Server 的 information\_schema.columns.extra 返回大写 AUTO\_INCREMENT，而 Peewee 4.3.0 使用小写字符串 auto\_increment 做大小写敏感包含判断。我在 Server 中只修改元数据展示，不改变真实自增机制；新安装、主库和普通数据库升级脚本都输出小写，回滚脚本恢复旧值，并增加历史 catalog 依赖保护。另外同一 PR 在 B 模式下使用 atttypmod 保留 DECIMAL 精度，同时限制作用范围，避免影响其他兼容模式。该 PR 的完整流水已通过并合入。

---

## 6. 元数据自检清单（和 Peewee 对齐）

用你的话解释下面三条各对应 Peewee 的什么行为：

1. `SHOW INDEX` 主键 `Key_name = PRIMARY` →
  Peewee 的 `get_primary_keys()` 才能过滤出该索引并取得主键列名；随后 `get_columns()` 才会设置 `primary_key=True`。

2. `information_schema.columns.extra = auto_increment` →
  Peewee 执行大小写敏感的 `'auto_increment' in extra` 判断，匹配后设置 `identity=True`。

3. `data_type/column_type` 与 `decimal(p,s)` →
  `data_type=decimal` 让 Peewee 和 MySQL 客户端识别为 MySQL DECIMAL 类型；`column_type=decimal(10,2)` 保留精度和小数位信息，避免只得到没有 `(p,s)` 的 `decimal`。

---

## 7. 产出与链接
| 类型             | 位置 / PR                                                                            | 状态                               |
| -------------- | ---------------------------------------------------------------------------------- | -------------------------------- |
| Plugin PR      | [Plugin #2553](https://gitcode.com/opengauss/Plugin/pull/2553)                     | open；当前 HEAD `40a33424`，等待最新完整流水 |
| Server PR      | [openGauss-server #9709](https://gitcode.com/opengauss/openGauss-server/pull/9709) | merged；HEAD `e377fb225`          |
| examples       | [examples #114](https://gitcode.com/opengauss/examples/pull/114)                   | open；当前 HEAD `847ac68`           |
| docs 开发指导      | [docs #8846](https://gitcode.com/opengauss/docs/pull/8846)                         | open / WIP；最终联合验证结果待更新           |
| Plugin 类型名映射基线 | Plugin #2551                                                                       | merged                           |
| 原始缺陷记录         | [Plugin Issue #1626](https://gitcode.com/opengauss/Plugin/issues/1626)             | 当前页面已关闭，保留为历史缺陷记录                |
| 实习任务           | [任务 #35](https://gitcode.com/opengauss/opensource-intership/issues/35)             | 未完成，继续统一跟踪                       |

**范围说明（防止过度承诺）：**

- 本工作是否关闭 Issue #1626 / 任务 #35？  
- 仍跟踪、未包含在 PR 里的问题有哪些？
* Issue #1626 页面此前已按老师当时要求关闭，但不能将其理解为全部验收完成；最终进度继续由实习任务 #35 跟踪。

* 任务 #35 当前仍不能关闭。

* 尚待完成：

  1. Plugin #2553 当前 HEAD 的完整流水成功并合入；
  2. 在包含已合入 Server #9709 和 Plugin #2553 的同一组合版本上，连续执行两轮全部 48 条测试，要求全部 PASS、0 SKIP；
  3. 将真实联合验证结果更新到 examples 自测报告；
  4. 更新 docs #8846 的最终验证结果、解除 WIP 并完成合入；
  5. examples #114 和 docs #8846 完成检视并合入。

* 当前测试还记录了不属于 #1626 已修复范围的差异：

  * 认证失败目前为 `514/InternalError`，并非标准 MySQL 常见的 `1045/OperationalError`；
  * 缺表目前为 `132/InternalError`；
  * 当前 PyMySQL 字符串转义组合要求连接用户设置 `standard_conforming_strings=off`。
 

---

## 8. 面试自测（合上笔记口答）

- [ ] 为什么不能只靠索引名文本判断主键？  
- [ ] 主键返回值为何要注意 SQL 类型？  
- [ ] Peewee 读不出自增时，元数据里哪个字段会错？  
- [ ] B 库里 schema 和 MySQL database 是什么关系？  
- [ ] native 密码解决什么问题？  
- [ ] 这两个修复合在一起，对 Peewee 意味着什么？  
- [ ] 如果面试官问“是不是 AI 写的”，你怎么答？  
- [ ] 你下一步若继续做，会优先修/测什么？  

**仍答不上来的题（优先补）：**

1.  
2.  

---

## 参考（只链路径，正文勿整段粘贴敏感信息）

- 官方开发指导：`docs/zh/mysql_compatibility_developer_guide/peewee_development.md`  
- Lite：`docs-lite/zh/mysql_compatibility_developer_guide/peewee_development.md`  
- 本地 PR 草稿：`D:\Codex\openEuler\Plugin-PR-1626草稿.md` 等  
- Peewee / PyMySQL 官方文档（按需）  
