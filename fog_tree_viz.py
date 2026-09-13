"""採用中のモデル(RF/XGBoost/LightGBM)が実際にどれくらいの深さの決定木を使っているかを
視覚的に示す図を生成するための、木構造の抽出・レイアウト計算部分。

木は深いほど複雑な条件分岐を表現できる(過学習しやすくもなる)一方、浅いと単純化
しすぎて精度が伸びない。この図はその「実際にどれだけ複雑な木が採用されたか」を
直感的に把握するためのもの。

RF/XGBoost/LightGBMはそれぞれ内部の木構造の持ち方が異なる(sklearn: tree_の配列、
XGBoost: get_dump()のJSON、LightGBM: dump_model()のtree_structure)ため、まず共通の
ノード形式({"depth", "is_leaf", "feature", "threshold", "left", "right", "value"})に
変換してから、どの方式でも同じ描画ロジック(webapp.py側)で統一したスタイルの図にする。
graphviz(dotコマンド・pythonパッケージとも)がサーバーに入っていないため、
xgboost.plot_tree/lightgbm.plot_treeは使わずmatplotlib単体で自前描画する
(実際の描画はwebapp.pyの_render_tree_depth_diagram_pngが行う。ここではデータ抽出・
レイアウト計算のみを行い、matplotlibには依存しない)。
"""
import numpy as np

# 深さがこれを超えると、それ以降は個々のノードを描かず「以降は省略」の1枚の葉として
# まとめる(深さ14のRF等をノード単位でそのまま描画すると、指数的にノード数が増えて
# 読めない画像になるため)。実際の最大深さ自体は別途タイトルに明記する。
DEFAULT_MAX_DISPLAY_DEPTH = 4


def sklearn_tree_to_nodes(tree, feature_names, node_id=0, depth=0):
    """sklearn DecisionTreeClassifier.tree_を共通のノード形式に変換する。"""
    left = tree.children_left[node_id]
    right = tree.children_right[node_id]
    if left == -1 and right == -1:
        counts = tree.value[node_id][0]
        cls = int(np.argmax(counts))
        return {"depth": depth, "is_leaf": True, "value": cls, "n": int(counts.sum())}
    feat_idx = tree.feature[node_id]
    feat_name = feature_names[feat_idx] if 0 <= feat_idx < len(feature_names) else f"特徴量{feat_idx}"
    return {
        "depth": depth, "is_leaf": False,
        "feature": feat_name, "threshold": float(tree.threshold[node_id]),
        "left": sklearn_tree_to_nodes(tree, feature_names, left, depth + 1),
        "right": sklearn_tree_to_nodes(tree, feature_names, right, depth + 1),
    }


def xgb_tree_to_nodes(node, feature_names, depth=0):
    """xgboost booster.get_dump(dump_format="json")[i]をjson.loadsした辞書を
    共通のノード形式に変換する。"""
    if "leaf" in node:
        return {"depth": depth, "is_leaf": True, "value": float(node["leaf"]), "n": None}
    split = node["split"]
    feat_idx = int(split[1:]) if split.startswith("f") and split[1:].isdigit() else None
    feat_name = (
        feature_names[feat_idx] if feat_idx is not None and 0 <= feat_idx < len(feature_names)
        else split
    )
    children_by_id = {c["nodeid"]: c for c in node.get("children", [])}
    yes_node = children_by_id.get(node.get("yes"))
    no_node = children_by_id.get(node.get("no"))
    return {
        "depth": depth, "is_leaf": False,
        "feature": feat_name, "threshold": float(node["split_condition"]),
        "left": xgb_tree_to_nodes(yes_node, feature_names, depth + 1) if yes_node else
                {"depth": depth + 1, "is_leaf": True, "value": 0.0, "n": None},
        "right": xgb_tree_to_nodes(no_node, feature_names, depth + 1) if no_node else
                 {"depth": depth + 1, "is_leaf": True, "value": 0.0, "n": None},
    }


def lgbm_tree_to_nodes(node, feature_names, depth=0):
    """lightgbm booster_.dump_model()["tree_info"][i]["tree_structure"]を
    共通のノード形式に変換する。"""
    if "leaf_value" in node:
        return {"depth": depth, "is_leaf": True, "value": float(node["leaf_value"]),
                "n": node.get("leaf_count")}
    feat_idx = node["split_feature"]
    feat_name = feature_names[feat_idx] if 0 <= feat_idx < len(feature_names) else f"特徴量{feat_idx}"
    return {
        "depth": depth, "is_leaf": False,
        "feature": feat_name, "threshold": float(node["threshold"]),
        "left": lgbm_tree_to_nodes(node["left_child"], feature_names, depth + 1),
        "right": lgbm_tree_to_nodes(node["right_child"], feature_names, depth + 1),
    }


def tree_actual_max_depth(node):
    """表示上の省略に関わらない、木の本当の最大深さ。"""
    if node is None:
        return -1
    if node["is_leaf"]:
        return node["depth"]
    return max(tree_actual_max_depth(node.get("left")), tree_actual_max_depth(node.get("right")))


def tree_node_count(node):
    if node is None or node["is_leaf"]:
        return 1
    return 1 + tree_node_count(node.get("left")) + tree_node_count(node.get("right"))


def assign_layout(node, max_display_depth=DEFAULT_MAX_DISPLAY_DEPTH):
    """各ノードの描画位置(x, y)を計算する。葉ノードを左から右へ0,1,2...と並べ、
    内部ノードのx座標は子ノードの平均にする(標準的な木構造レイアウト)。yはdepthそのもの
    (深いほど下)。max_display_depthを超えた部分は個々のノードを展開せず「以降は省略」の
    1枚の葉として扱う(実際の分岐がまだ続いていてもここで打ち切る)。
    戻り値: {id(node辞書): (x, y, truncated)}のdict。呼び出し側でnodeオブジェクトの
    idをキーに位置を引く。
    """
    positions = {}
    leaf_counter = [0]

    def walk(n):
        if n is None:
            return 0.0
        truncated = (not n["is_leaf"]) and n["depth"] >= max_display_depth
        if n["is_leaf"] or truncated:
            x = float(leaf_counter[0])
            leaf_counter[0] += 1
            positions[id(n)] = (x, n["depth"], truncated)
            return x
        lx = walk(n["left"])
        rx = walk(n["right"])
        x = (lx + rx) / 2.0
        positions[id(n)] = (x, n["depth"], False)
        return x

    walk(node)
    return positions
