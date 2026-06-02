# Polymarket 真实交易接入指南

## 概述

Polymarket 使用 **CLOB (Central Limit Order Book)** 进行链上交易，基于 **Polygon** 链。交易流程：

```
私钥签名 → 派生 API 凭证 → 创建存款钱包 → 存入 USDC → 下单交易
```

---

## 前置准备

### 1. 创建 Polygon 钱包

```bash
# 使用 MetaMask 或任意钱包工具创建 Polygon 钱包
# 获取私钥（0x 开头的 64 位十六进制字符串）

export POLY_PRIVATE_KEY="0xyour_private_key_here"
```

**安全警告**: 私钥是钱包的唯一凭证，切勿泄露或提交到版本控制。

### 2. 充值 USDC

在 Polygon 网络上向钱包地址充值 **USDC**（建议至少 $100 起）。

获取 USDC 的方式：
- 从交易所（Binance/OKX）提现到 Polygon 网络
- 通过 [Polygon Bridge](https://portal.polygon.technology/bridge) 从以太坊桥接
- 通过 [Polymarket Bridge](https://bridge.polymarket.com) 从其他链充值

---

## 接入步骤

### 步骤 1: 生成 API 凭证（一次性）

```bash
cd /home/lyrobo/btc-polymarket-predictor
export POLY_PRIVATE_KEY="0x..."

/usr/bin/python3 real_trader.py --setup
```

输出示例：
```
🔑 API Credentials Generated
============================================================
  API Key:      550e8400-e29b-41d4-a716-446655440000
  API Secret:   base64EncodedSecretString...
  Passphrase:   randomPassphraseString...

Save these! Add to your .env file:
  export POLY_API_KEY=550e8400-e29b-41d4-a716-446655440000
  export POLY_API_SECRET=base64EncodedSecretString...
  export POLY_API_PASSPHRASE=randomPassphraseString...
============================================================
```

### 步骤 2: 获取存款钱包地址

```bash
/usr/bin/python3 real_trader.py --wallet
```

输出：
```
📬 Deposit Wallet Address: 0x1234...abcd
   Transfer USDC to this address to fund trading.
```

### 步骤 3: 存入 USDC

将 USDC 转账到上面显示的 **存款钱包地址**。

- 网络: Polygon (Chain ID: 137)
- 代币: USDC (PoS)
- 地址: 步骤 2 输出的地址
- 建议金额: $100-$500 起步

### 步骤 4: 验证状态

```bash
export POLY_PRIVATE_KEY="0x..."
export POLY_API_KEY="..."
export POLY_API_SECRET="..."
export POLY_API_PASSPHRASE="..."
export POLY_DEPOSIT_WALLET="0x..."

/usr/bin/python3 real_trader.py --status
```

### 步骤 5: 开始交易

```bash
# 单次测试
/usr/bin/python3 real_trader.py --once

# 持续交易（每 30 秒）
/usr/bin/python3 real_trader.py --interval 30
```

---

## 环境变量配置

创建 `.env` 文件：

```bash
# ~/.polymarket.env
POLY_PRIVATE_KEY=0xyour_private_key_here
POLY_API_KEY=your_api_key
POLY_API_SECRET=your_api_secret
POLY_API_PASSPHRASE=your_passphrase
POLY_DEPOSIT_WALLET=0xdeposit_wallet_address
```

使用方式：

```bash
source ~/.polymarket.env
/usr/bin/python3 real_trader.py --status
```

---

## 交易机制

### 下单流程

```
预测引擎 → Edge 检测 → Kelly 仓位计算 → 创建订单 → EIP-712 签名 → 提交 CLOB
```

### 订单类型

| 类型 | 说明 | 用途 |
|------|------|------|
| **GTC** | Good-Til-Cancelled | 限价单，默认 |
| **GTD** | Good-Til-Date | 到期自动取消 |
| **FOK** | Fill-Or-Kill | 全部成交或取消 |
| **FAK** | Fill-And-Kill | 部分成交或取消剩余 |

### 费用

- **交易费**: 约 2%（taker）/ 0%（maker，有返佣）
- **Gas 费**: Polygon 链上结算，约 $0.01-0.05/笔
- **滑点**: 5 分钟市场流动性较低，建议用限价单

### 风险控制

- **Kelly 仓位**: 最大 15% 资金/笔
- **最小 Edge**: 3% 才开仓
- **最小置信度**: 55%
- **最小下单**: $5 USDT

---

## API 架构

Polymarket 提供三个 API：

| API | 地址 | 用途 | 认证 |
|-----|------|------|------|
| **Gamma API** | `gamma-api.polymarket.com` | 市场发现、事件查询 | 无需认证 |
| **Data API** | `data-api.polymarket.com` | 用户持仓、交易记录 | 无需认证 |
| **CLOB API** | `clob.polymarket.com` | 订单簿、下单、撤单 | L1+L2 认证 |

### 认证模型

```
L1 (私钥) → 创建/派生 API 凭证 (L2)
                      ↓
L2 (API Key + Secret + Passphrase) → HMAC-SHA256 签名 → 下单/查询
```

### Python SDK

```python
from py_clob_client_v2 import ClobClient, ApiCreds, OrderArgs, OrderType

# 初始化
client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=os.getenv("POLY_PRIVATE_KEY"),
    creds=ApiCreds(
        api_key=os.getenv("POLY_API_KEY"),
        api_secret=os.getenv("POLY_API_SECRET"),
        api_passphrase=os.getenv("POLY_API_PASSPHRASE"),
    ),
    signature_type=3,  # POLY_1271
    funder=os.getenv("POLY_DEPOSIT_WALLET"),
)

# 下单
response = client.create_and_post_order(
    OrderArgs(
        token_id="TOKEN_ID",
        price=0.50,
        size=10,
        side=BUY,
    ),
    options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
    order_type=OrderType.GTC,
)
```

---

## 常见问题

### Q: 如何提现 USDC？

通过 [Polymarket Bridge](https://bridge.polymarket.com) 或 API：
```bash
# 创建提现地址
curl -X POST "https://bridge.polymarket.com/create-withdrawal-addresses" \
  -H "Content-Type: application/json" \
  -d '{"destinationAddress": "your_wallet_address"}'
```

### Q: 订单未成交怎么办？

- 限价单可能未成交（价格不在市场范围内）
- 使用 `client.cancel_order(order_id)` 取消
- 使用 `client.cancel_all()` 取消所有订单

### Q: 如何查询订单状态？

```python
order = client.get_order(order_id)
print(order)
```

### Q: 模拟交易和真实交易的区别？

| 项目 | 模拟交易 | 真实交易 |
|------|----------|----------|
| 资金 | 虚拟 $100 | 真实 USDC |
| 结算 | 概率模拟 | 链上结算 |
| 风险 | 无 | 真实资金风险 |
| 费用 | 无 | 2% 手续费 + Gas |
| 流动性 | 无限制 | 受市场深度限制 |

### Q: 5 分钟市场流动性如何？

- BTC 5 分钟市场流动性中等（日均 $10k-$50k）
- 大额订单（>$500）可能产生滑点
- 建议单笔不超过 $100
- 使用限价单而非市价单

---

## 安全建议

1. **专用钱包**: 为 Polymarket 交易创建独立钱包，不要使用主钱包
2. **限制资金**: 只存入交易所需资金，不要存大量 USDC
3. **API 凭证安全**: 不要将 API 凭证提交到 Git
4. **监控交易**: 定期检查 `real_trades` 表确认交易记录
5. **设置止损**: 考虑在策略中加入最大回撤限制

---

## 文件清单

| 文件 | 说明 |
|------|------|
| `real_trader.py` | 真实交易模块（SDK 集成） |
| `sim_trader.py` | 模拟交易模块 |
| `websocket_collector.py` | OKX WebSocket 数据 |
| `unified_strategy.py` | 四方向融合策略 |
| `realtime_service.py` | Edge 检测引擎 |
| `dashboard_v3.py` | 仪表盘（端口 8765） |

---

## 下一步

1. 创建 Polygon 钱包并充值 USDC
2. 运行 `--setup` 生成 API 凭证
3. 运行 `--wallet` 获取存款地址
4. 向存款地址转账 USDC
5. 运行 `--status` 验证连接
6. 运行 `--once` 测试单次交易
7. 确认无误后运行 `--interval 30` 持续交易

**⚠️ 警告**: 真实交易涉及真实资金风险。建议先用模拟交易验证策略至少 24 小时，确认胜率稳定后再投入真实资金。
