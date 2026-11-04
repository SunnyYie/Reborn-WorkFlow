"""One-time curation, checked against all four rendered PDF pages, 2026-11-03."""
from pathlib import Path
import json
import rag


def section(label, *lines):
    return {"label": label, "lines": list(lines)}


def route(labels, cues):
    return [{"label": label, "cue": cue, "evidence": "visually_checked_pdf_table"}
            for label, cue in zip(labels.split(), cues)]


def apply(dbpath):
    db = rag.connect(dbpath)
    filename = "2026.05.10-歌单(祢爱不变，祢是我的一切，别无他名) V3.pdf"
    records = [json.loads(r[0]) for r in db.execute("SELECT payload FROM songs")
               if json.loads(r[0])["source_name"] == filename]
    for r in records:
        checked = rag.get_record(db, r["id"])
        if checked["citation"]["sha256"] != "a06b371437cb74d36edb62649609be79f3358bbd368f0b15ec4b4fd3e17763ec":
            raise ValueError("PDF不是已逐页核对的版本，拒绝套用引导修订")
    db.close()
    for r in records:
        title, role = r["title"], r["role"]
        if role == "response":
            # Bare response title does not prove an identical arrangement.
            continue
        if role == "ending":
            patch = {"route": route("IN V1 C1 V2 C2", ["4", "All", "All", "All", "All"]),
                     "lyric_sections": [], "issues": []}
        elif title == "祢爱不变":
            v1 = section("V1", "超过我所面对的高山", "胜过死亡阴间的挑战", "跨过所有变动和艰难", "祢爱不变 存到永远")
            v2 = section("V2", "祢的爱一直都在追赶着我", "渗透我灵魂超过所想所求", "我永远不再害怕孤单承受", "祢爱不变 存到永远")
            chorus = ["祢爱不失败 永不放弃", "永远不会离开"] * 3
            patch = {
                "route": route("IN V1 C1 V1 C2 V2 C2 IN B C2 IN V2",
                               ["4", "男", "All", "All", "All", "男高8度 All", "All", "4", "All", "All", "Tag All", "All"]),
                "lyric_sections": [v1, v2, section("C1", *chorus),
                                   section("C2", *chorus, "祢的爱"),
                                   section("B", "或生或死", "我深知我都隐藏在祢爱的大能中", "我得赦免",
                                           "再没有任何事能让我与祢爱隔绝"),
                                   section("TAG", "祢的爱追赶着我 x4")],
                "issues": ["第11格同时标注 IN 与 Tag；需乐手确认关系，未擅自改成两段"]}
        elif title == "祢是我的一切":
            patch = {
                "route": route("IN V1 C V2 C IN C C C END",
                               ["4", "崔", "女", "刘 男和", "女 男和", "8 经文", "女 男和", "女 男和", "All", "女 轻"]),
                "lyric_sections": [
                    section("V1", "祢是我依靠的力量", "祢是我寻求的宝藏", "祢是我的一切",
                            "祢好比贵重的珍宝", "我怎能放弃祢不要", "祢是我的一切"),
                    section("V2", "担当我罪债和羞辱", "死里复活我蒙救赎", "祢是我的一切",
                            "当我跌倒祢扶持我", "当我干渴祢充满我", "祢是我的一切"),
                    section("C", "耶稣 神羔羊 配受大赞美", "耶稣 神羔羊 配受大赞美"),
                    section("END", "配受大赞美 x2")], "issues": []}
        else:
            patch = {
                "route": route("IN V1 V2 C V3 C IN B B C END",
                               ["4", "David边", "男", "All", "All", "All", "8", "All", "All", "All", "All"]),
                "lyric_sections": [
                    section("V1", "祂名 高过万名之上", "荣耀 超越地久天长", "颂赞 在宇宙中回响", "我心 也一同唱", "祂名 至高无上"),
                    section("V2", "荣光 比太阳更明亮", "恩典 如祂大爱宽广", "医治 在祂掌权翅膀", "祂是 万王之王", "祂名 至高无上"),
                    section("V3", "盼望 穿透世间迷惘", "看哪 十架已经得胜", "复活 大能吞灭死亡", "生命 在祂名里", "至高无上的名"),
                    section("C", "君王降临 我举目仰望", "世界真光 将我来寻找", "世上别无他名",
                            "世上别无他名", "基督 我的神", "祂已得胜 坐在宝座上", "众山跪拜 我们高举祂",
                            "世上别无他名", "世上别无他名", "基督 我的神（最后一遍C不唱）"),
                    section("B", "全地震动 在祂面前战抖", "锁链断开 当天地齐高唱", "圣洁是祂名 圣洁是祂名", "喔 耶稣 耶稣 耶稣"),
                    section("END", "世上别无他名", "世上别无他名 耶稣")], "issues": []}
        rag.review(dbpath, r["id"], patch,
                   "2026-11-03 Codex逐页对照用户PDF渲染图与文字层，核对表格、双栏歌词和明确段落标记。"
                   "这是来源提取核对，不代表本次现场编排已由用户确认。")


if __name__ == "__main__":
    import sys
    apply(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent/"data"/"songs.sqlite3")
