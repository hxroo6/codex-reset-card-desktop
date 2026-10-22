# Codex Reset Card Desktop · Windows 桌面增强版

为 [codex-reset-card](https://github.com/loccen/codex-reset-card) 提供中文桌面面板、卡片提醒和右下角倒计时。当前为预览版，已验证上游版本为 **0.2.0**。

## 功能

- 卡片有效期、自动使用计划、倒计时、后台与授权状态。
- 右下角悬浮窗，显示和置顶均可独立开关，设置自动保存。
- 单卡每次推迟 2 分钟，最迟到期前 1 分钟；后台执行中或状态不明时禁止调整。
- 到期提醒、使用结果与异常提醒；查询失败保留快照并标记陈旧。
- 独立演示模式，不连接账号、不操作真实卡片。

## 快速开始

需要 Windows 10/11、Python 3.12+（包含 tkinter）、Node.js 20+，以及已经登录 ChatGPT 的 Codex CLI。

```powershell
npm install -g codex-reset-card@0.2.0
codexrc config set lead-time 5m
codexrc service install
python .\desktop\reset_card_panel.py
```

安装服务不会授权使用卡片。需要自动使用时，由你单独执行 `codexrc arm`；停用时执行 `codexrc disarm`。授权后会处理当前和未来发现的卡片。电脑需开机、用户保持登录、网络和登录有效。

仅查看演示：

```powershell
python .\desktop\reset_card_panel.py --demo
```

## 当前边界

- 仅支持 Windows；原后台默认每 24 小时发现新卡，面板每 5 分钟在线查看，不会因此更新后台发现计划。
- 推迟按钮会短暂停止后台、持有同名互斥锁、核对并原子更新这一张卡的计划，再恢复后台。它不执行立即兑换。
- 主面板最小化后仍提醒；关闭面板后提醒停止，原调度服务继续运行。
- 当前源码快照的双击启动器使用固定 Python 路径；也可直接使用上面的 `python` 命令运行。通用启动与兼容检查将继续完善。
- 不要提交本机日志、登录文件、卡片状态或真实截图。

## 测试

```powershell
cd desktop
python -m unittest -v
python .\reset_card_panel.py --smoke-test
```

测试使用模拟数据和临时目录。窗口冒烟在演示模式中验证显示、置顶、提醒和延期上限，不消费真实卡片。

## 许可证与来源

本仓库采用 MIT。上游项目版权与许可证见 [NOTICE](NOTICE) 和 [原项目 MIT 许可](LICENSES/codex-reset-card.MIT)。这是独立社区工具，与 OpenAI 无隶属关系。