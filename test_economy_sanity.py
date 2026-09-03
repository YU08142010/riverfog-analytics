"""ポイント経済の確率・期待値まわりの回帰テスト。

2026-09-03に、福袋(ガチャ)の当たり確率が設計上の1/5(20%)ではなく実質100%近くまで
壊れていたバグが見つかった(アイテムの重みを確率計算に混ぜてしまい、価格帯が広いほど
確率が歪む作りだった)。原因コードの修正だけでなく、同種の「確率・期待値の設計と実装が
ズレている」バグを次に導入したときに気づけるよう、この回帰テストを置く。

pytest等の外部依存を増やしたくないため、単純なアサーション+モンテカルロで書く。
実行: /opt/river-fog/.venv/bin/python3 test_economy_sanity.py
"""
import random
import sys

import webapp

N_TRIALS = 20000
TOLERANCE = 0.03  # 20%±3ポイントの範囲に収まっていればOK(N=20000なら統計的に十分な余裕)


def make_weighted(costs):
    """category/cosmetic_category/labelはこの検証では使わないのでダミーでよい。"""
    return [("cat", "cat", "label", {"id": c, "cost": c}, 1.0 / max(c, 1)) for c in costs]


def jackpot_rate(weighted, n=N_TRIALS, seed=0):
    rng = random.Random(seed)
    orig_random = random.random
    random.random = rng.random
    try:
        hits = sum(1 for _ in range(n) if webapp._gacha_is_jackpot(weighted))
    finally:
        random.random = orig_random
    return hits / n


def check(label, rate, expected):
    ok = abs(rate - expected) <= TOLERANCE
    status = "OK" if ok else "NG"
    print(f"[{status}] {label}: observed={rate:.3f} expected={expected:.3f} (tolerance={TOLERANCE})")
    return ok


def main():
    expected_rate = 1.0 / webapp.GACHA_BIG_REFUND_WEIGHT
    results = []

    # 実店舗と同じ価格帯(500pt〜200万pt)。この分布で壊れていたのが今回のバグ。
    real_costs = [500, 1200, 1500, 1800, 2000, 2200, 2500, 3000, 3500, 4000, 5000,
                  6000, 8000, 10000, 15000, 20000, 25000, 50000, 1000000]
    results.append(check("実際の価格帯(500〜200万pt)", jackpot_rate(make_weighted(real_costs)), expected_rate))

    # 極端に安いアイテムだけ(重みが最大化されるケース)。
    results.append(check("激安アイテムのみ(価格1のもの100件)", jackpot_rate(make_weighted([1] * 100)), expected_rate))

    # 極端に高いアイテムだけ(重みが最小化されるケース、バグ発生時に実質100%になっていたパターン)。
    results.append(check("超高額アイテムのみ(200万pt×20件)", jackpot_rate(make_weighted([2000000] * 20)), expected_rate))

    # 候補が1件だけ。
    results.append(check("候補1件のみ", jackpot_rate(make_weighted([10000])), expected_rate))

    # 候補が空(全部所持済み) -> 必ず当たり(参加賞)扱いになるのが仕様。
    results.append(check("候補なし(全部所持済み)", jackpot_rate([]), 1.0))

    if not all(results):
        print("\nFAIL: 福袋の当たり確率が設計値からズレています。webapp._gacha_is_jackpot()を確認してください。")
        sys.exit(1)
    print("\nPASS: 福袋の当たり確率は全ケースで設計値どおりでした。")


if __name__ == "__main__":
    main()
