"""LSTM 购买序列模型（参考「GitHub LSTM算法」改写，独立可跑的离线脚本）。

参考项目（天池 / Kaggle「用户购买行为预测」）用「用户历史购买序列」预测下一件商品：
    Embedding(商品) + 商品流行度 → 双层 LSTM → 注意力池化 → 全连接 → Top-K 商品
本脚本把它的架构搬到本商城：数据来源于 cs.order（user_id / product_id / 下单时间），
训练完输出 Top-K 预测，供「智能分析」参考。

⚠️ 重要：本商城订单量极小（当前个位数），**不足以训练有效的序列模型**，
脚本会在样本过少时直接提示并退出，不会给出误导性结论。
等订单量积累到千级 / 万级（每用户 ≥ 6 次购买）后再运行本脚本才有意义。

运行（可选依赖：torch / pandas / numpy / scikit-learn / matplotlib）：
    .venv/bin/pip install torch pandas numpy scikit-learn matplotlib tqdm
    .venv/bin/python 智能分析/LSTM购买序列.py

只依赖标准库 + 项目内模块读取数据；上面的依赖仅本脚本需要，线上页面不用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MIN_SEQ = 6          # 每个用户至少要有这么多次购买才构造成样本
MIN_SAMPLES = 200    # 样本总数低于此值就不训练（训练没意义）
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# 1. 读取本商城订单，构造成「用户购买序列」
def load_sequences():
    """从 cs.order 读取并构造 {user_id: [商品ID序列（按下单时间升序）]}。"""
    from 公共.db_config import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, product_id, goodsname, create_time FROM `order` "
                "WHERE user_id IS NOT NULL AND user_id <> '' ORDER BY create_time ASC"
            )
            rows = cur.fetchall() or []

    sequences = {}
    for r in rows:
        uid = str(r.get("user_id") or "").strip()
        item = str(r.get("product_id") or "").strip() or str(r.get("goodsname") or "").strip()
        if not uid or not item:
            continue
        sequences.setdefault(uid, []).append(item)
    return sequences

def build_samples(sequences):
    """把每个用户的序列切分成 (前缀, 下一个商品) 样本对。"""
    X, Y = [], []
    for seq in sequences.values():
        if len(seq) < MIN_SEQ:
            continue
        for i in range(MIN_SEQ - 1, len(seq)):
            X.append(seq[:i])
            Y.append(seq[i])
    return X, Y

def report(sequences):
    users = len(sequences)
    orders = sum(len(v) for v in sequences.values())
    eligible = sum(1 for v in sequences.values() if len(v) >= MIN_SEQ)
    X, Y = build_samples(sequences)
    print("=" * 56)
    print("数据现状（来自 cs.order）")
    print(f"  下单用户数       : {users}")
    print(f"  订单条数         : {orders}")
    print(f"  满足 >= {MIN_SEQ} 次购买的用户 : {eligible}")
    print(f"  可构造训练样本   : {len(X)}")
    print("=" * 56)
    return X, Y

# 2. 训练（仅在数据足够时执行）
def train(X, Y, item_to_idx, num_items):
    import time
    import numpy as np
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import Dataset, DataLoader

    torch.manual_seed(42)
    np.random.seed(42)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"使用设备：{device}")

    MAX_LEN, EMB, HID, LAYERS = 40, 64, 128, 2
    BATCH, EPOCHS, LR, TOPK = 128, 60, 5e-4, 20

    class SeqDataset(Dataset):
        def __init__(self, X, Y):
            self.X, self.Y = X, Y

        def __len__(self):
            return len(self.X)

        def __getitem__(self, i):
            seq = self.X[i][-MAX_LEN:]
            pad = MAX_LEN - len(seq)
            ids = [0] * pad + [item_to_idx.get(str(x), 0) for x in seq]
            return (torch.tensor(ids, dtype=torch.long),
                    torch.tensor(item_to_idx.get(str(self.Y[i]), 0), dtype=torch.long))

    class Predictor(nn.Module):
        def __init__(self):
            super().__init__()
            self.embedding = nn.Embedding(num_items + 1, EMB, padding_idx=0)
            self.lstm = nn.LSTM(EMB, HID, LAYERS, batch_first=True, dropout=0.3)
            self.attention = nn.Linear(HID, 1)
            self.fc = nn.Sequential(nn.LayerNorm(HID), nn.Linear(HID, HID), nn.GELU(),
                                    nn.Linear(HID, num_items + 1))
            self.dropout = nn.Dropout(0.3)

        def forward(self, x):
            emb = self.embedding(x)
            out, _ = self.lstm(emb)
            w = torch.softmax(self.attention(out), dim=1)
            ctx = torch.sum(w * out, dim=1)
            return self.fc(self.dropout(ctx))

    # 训练 / 验证切分
    n = len(X)
    split = int(n * 0.9)
    train_ds, val_ds = SeqDataset(X[:split], Y[:split]), SeqDataset(X[split:] or X[:1], Y[split:] or Y[:1])
    train_loader = DataLoader(train_ds, batch_size=BATCH, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=BATCH)

    model = Predictor().to(device)
    crit = nn.CrossEntropyLoss(ignore_index=0)
    opt = optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)

    def mrr():
        model.eval()
        rr = []
        with torch.no_grad():
            for xb, yb in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                topk = torch.topk(model(xb), TOPK, dim=1).indices
                for i in range(len(yb)):
                    t = yb[i].item()
                    if t == 0:
                        continue
                    hit = (topk[i] == t).nonzero()
                    rr.append(1.0 / (hit[0].item() + 1) if len(hit) else 0.0)
        return sum(rr) / len(rr) if rr else 0.0

    best = 0.0
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    model_path = os.path.join(OUTPUT_DIR, "best_model.pth")
    for epoch in range(EPOCHS):
        model.train()
        total = 0.0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = crit(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total += loss.item()
        score = mrr()
        print(f"Epoch {epoch+1:>3}/{EPOCHS}  loss={total/max(1,len(train_loader)):.4f}  MRR@{TOPK}={score:.4f}")
        if score >= best:
            best = score
            torch.save(model.state_dict(), model_path)
    print(f"最佳 MRR@{TOPK} = {best:.4f}，模型已保存到 {model_path}")
    return best

def main():
    sequences = load_sequences()
    X, Y = report(sequences)
    if len(X) < MIN_SAMPLES:
        print()
        print("⚠️  样本太少，不进行训练。")
        print(f"   当前仅 {len(X)} 个样本（门槛 {MIN_SAMPLES}）。")
        print("   原因：本商城订单量太小，训练出的 LSTM 会严重过拟合、结论不可信。")
        print("   建议：继续积累订单（每个用户 >= 6 次购买）后再运行本脚本；")
        print("   现阶段线上页面用的是可解释的轻量算法（智能分析/购买预测.py）。")
        return
    try:
        import torch  # noqa: F401
    except ImportError:
        print("缺少依赖：请先执行  .venv/bin/pip install torch pandas numpy scikit-learn matplotlib tqdm")
        return
    items = sorted({str(x) for seq in sequences.values() for x in seq} | {str(y) for y in Y})
    item_to_idx = {it: i + 1 for i, it in enumerate(items)}
    train(X, Y, item_to_idx, len(items))

if __name__ == "__main__":
    main()