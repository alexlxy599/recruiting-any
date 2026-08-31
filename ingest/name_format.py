"""姓名显示规范化。

库里 first_name/last_name 的存法不统一,直接 f"{first} {last}" 会出三种错:
  1. 中文名顺序反了      first=赫 last=张        → 显示"赫 张",应为"张赫"
  2. 拼音名+中文全名并存  first=Zijian last=赵子建 → 显示"Zijian 赵子建",应为"赵子建"
  3. 导入截断错位        first=ue Cui崔雪 last=X → 首字母被切进 last_name,应为"崔雪"

只改显示,不改库。库里的原值保留,便于回溯来源。
"""
import re

CJK = re.compile(r"[一-鿿]")
CJK_RUN = re.compile(r"[一-鿿]{2,}")


def display_name(first, last):
    f = (first or "").strip()
    l = (last or "").strip()
    if not f and not l:
        return ""
    if not f:
        return l
    if not l:
        return f

    f_cjk, l_cjk = bool(CJK.search(f)), bool(CJK.search(l))

    # 情况 3:last_name 只剩一个拉丁字母 —— 导入把首字母切走了,拼回去
    if len(l) == 1 and l.isascii() and l.isalpha():
        merged = l + f
        # 括注是标记不是名字（"（应届）""(Y)"），找中文名时要先剥掉
        bare = re.sub(r"[（(][^）)]*[）)]", "", merged)
        m = CJK_RUN.search(bare)
        return m.group() if m else merged.strip()

    # 情况 2:last_name 是完整中文名,first_name 是拼音 —— 用中文名
    if l_cjk and not f_cjk and len(l) >= 2:
        return l

    # 情况 1:纯中文 —— 姓在前,不加空格
    if f_cjk and l_cjk:
        return f"{l}{f}"

    # 西文名:名在前
    return f"{f} {l}"


if __name__ == "__main__":
    cases = [
        ("赫", "张", "张赫"),
        ("韶慧", "郭", "郭韶慧"),
        ("Zijian", "赵子建", "赵子建"),
        ("Yantao", "沈岩涛", "沈岩涛"),
        ("anjun Wang（应届）", "Y", "Yanjun Wang（应届）"),
        ("ue Cui崔雪", "X", "崔雪"),
        ("unshu Du杜云舒", "Y", "杜云舒"),
        ("aipeng 蔡", "H", "Haipeng 蔡"),
        ("Meng-Jiun", "Chiou", "Meng-Jiun Chiou"),
        ("Kai", "Zhao", "Kai Zhao"),
        ("", "Madonna", "Madonna"),
    ]
    ok = True
    for f, l, want in cases:
        got = display_name(f, l)
        flag = "ok " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {flag} ({f!r}, {l!r}) → {got!r}" + ("" if got == want else f"  期望 {want!r}"))
    print("\n全部通过" if ok else "\n有用例未通过")
