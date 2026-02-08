import time
import base64
import requests
import json
import streamlit as st
from urllib.parse import urlsplit, urlunsplit

API_URL_DEFAULT = "http://127.0.0.1:8000/api/v1"

st.set_page_config(page_title="XHS High Fidelity", layout="wide")

st.title("小红书洗稿工作台")
st.caption("输入小红书链接 → 自动采集原稿图文 → 生成洗稿文案 + 仿写图片（产品像素级保真）。")

api_url = API_URL_DEFAULT

tab_rewrite = st.tabs(["洗稿"])[0]


def _b64_to_bytes(b64: str) -> bytes:
    if not b64:
        return b""
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    return base64.b64decode(b64)


def _redact_url(u: str) -> str:
    # Avoid leaking query tokens (xsec_token etc.) in any UI/error output.
    try:
        sp = urlsplit(u)
        return urlunsplit((sp.scheme, sp.netloc, sp.path, "", ""))
    except Exception:
        return u


@st.cache_data(ttl=900, show_spinner=False)
def _fetch_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=20)
        if not r.ok:
            return None
        return r.content
    except Exception:
        return None


def _show_result(result: dict, title: str = "结果"):
    st.subheader(title)
    left, right = st.columns([1, 1])

    with left:
        st.caption("分析 / 元信息")
        if result.get("analysis"):
            st.json(result.get("analysis"), expanded=False)
        if result.get("scene_analysis"):
            st.json(result.get("scene_analysis"), expanded=False)
        if result.get("artifacts_dir"):
            st.code(result.get("artifacts_dir"))

    with right:
        # Prefer URL (smaller payload). Fallback to base64 if present.
        img_url = result.get("image_url")
        img_b64 = result.get("image_base64_png") or result.get("image_base64")
        if img_url:
            full = api_url.replace("/api/v1", "") + img_url
            data = _fetch_bytes(full)
            if data:
                st.image(data, use_container_width=True)
                st.download_button("下载 PNG", data=data, file_name="result.png", mime="image/png")
            else:
                st.caption(f"图片加载失败：{_redact_url(full)}")
        elif img_b64:
            img_bytes = _b64_to_bytes(img_b64)
            st.image(img_bytes, use_container_width=True)
            st.download_button(
                "下载 PNG",
                data=img_bytes,
                file_name="xhs_result.png",
                mime="image/png",
            )
        else:
            st.warning("结果中没有 image_url / image_base64")


# (Removed) 单张出图 / 批量 Flow 页面：当前只保留洗稿功能


with tab_rewrite:
    st.subheader("洗稿工作台（固定模板框架）")
    st.caption("你可以只粘贴小红书链接/分享文案 → 自动采集原稿；再填产品信息 → 生成 1 篇同字数级别的新稿。")

    st.markdown("**输入小红书链接**")
    source_text = st.text_area(
        "小红书分享文案/链接",
        height=80,
        key="xhs_source_text",
        placeholder="粘贴包含 xiaohongshu.com 的链接即可",
    )

    if st.button("采集原稿", key="btn_xhs_extract", type="primary"):
        if not source_text.strip():
            st.warning("请先粘贴小红书链接/分享文案")
        else:
            with st.spinner("采集中（可能需要几十秒）..."):
                resp = requests.post(
                    f"{api_url}/xhs/extract",
                    data={"source_text": source_text},
                    timeout=300,
                )
                if resp.status_code == 429:
                    st.warning("当前可能触发小红书风控（300012/IP 风险）：建议切换网络。")
                if not resp.ok:
                    st.error(f"采集失败: {resp.status_code}")
                    st.code(resp.text)
                else:
                    data = resp.json()
                    # Prefer reference_text (title + content) so正文不为空的概率更高
                    ref_text = (data.get("reference_text") or "").strip()
                    content = (data.get("content") or "").strip()
                    title = (data.get("title") or "").strip()
                    # Keep a single textbox that includes "title + content" (no separate title UI).
                    if ref_text:
                        st.session_state["rw_orig"] = ref_text
                    else:
                        joined = f"{title}\n{content}".strip()
                        st.session_state["rw_orig"] = joined or content
                    st.session_state["xhs_image_urls"] = data.get("image_urls", [])
                    st.success(f"采集成功：插图 {data.get('image_count', 0)} 张")

    # 采集结果展示（原稿插图 + 原稿全文）
    urls = st.session_state.get("xhs_image_urls") or []
    if urls:
        st.caption("原稿插图")
        from urllib.parse import quote

        cols = st.columns(5)
        for i, u in enumerate(urls):
            with cols[i % 5]:
                proxied = f"{api_url}/xhs/image?url={quote(u, safe='')}"
                data = _fetch_bytes(proxied)
                if data:
                    st.image(data, use_container_width=True)
                else:
                    st.caption("图片加载失败")

    st.caption("原稿全文")
    original_text = st.text_area("原稿全文", height=280, key="rw_orig", label_visibility="collapsed")

    submitted = st.button("生成洗稿", type="primary")
    template = ("LIST_REVIEW", "默认")
    product_name = ""
    product_features = ""

    if submitted:
        if not original_text.strip():
            st.warning("请先采集原稿或粘贴原稿全文")
        else:
            with st.spinner("生成中..."):
                resp = requests.post(
                    f"{api_url}/rewrite",
                    data={
                        "template_id": template[0],
                        "product_name": product_name,
                        "product_features": product_features,
                        "original_text": original_text,
                    },
                    timeout=180,
                )
                if not resp.ok:
                    st.error(f"失败: {resp.status_code}")
                    st.code(resp.text)
                else:
                    out = resp.json()
                    st.session_state["rewrite_out"] = out
                    st.success("洗稿已生成（结果已保存到页面状态）")
                    st.rerun()

    out = st.session_state.get("rewrite_out")
    if out:
        st.subheader(out.get("title", ""))
        st.write(f"字数: {out.get('word_count')} (目标: {out.get('target_word_count')})")
        outline = out.get("outline") or []
        if outline:
            st.write("大纲")
            st.write(outline)
        st.text_area("正文", value=out.get("content", ""), height=360)
        cover_text = out.get("cover_text") or []
        if cover_text:
            st.write("封面文案候选")
            st.write(cover_text)
        if out.get("hashtags"):
            st.write("标签")
            st.write(" ".join(out.get("hashtags")))

        st.divider()
        st.subheader("配图仿写")
        urls = st.session_state.get("xhs_image_urls") or []
        if not urls:
            st.info("需要先采集到插图，才能生成配图")
        else:
            style_preset = st.selectbox("B 风格", [("ugc","素人实拍"),("glossy","精修精美")], format_func=lambda x: x[1], index=0)
            if st.button("生成图片", key="btn_ab_images"):
                with st.spinner("生成配图中（B 会更慢）..."):
                    try:
                        resp2 = requests.post(
                            f"{api_url}/ab_images",
                            data={
                                "image_urls_json": json.dumps(urls, ensure_ascii=False),
                                "title": out.get("title", ""),
                                "bullets_json": json.dumps(outline[:6], ensure_ascii=False),
                                "style_preset": style_preset[0],
                            },
                            timeout=900,
                        )
                    except Exception as e:
                        st.error(f"生成图片请求失败：{e}")
                        resp2 = None

                    if resp2 is None:
                        pass
                    elif not resp2.ok:
                        st.error(f"配图失败: {resp2.status_code}")
                        st.code(resp2.text)
                    else:
                        data2 = resp2.json()
                        st.session_state["ab_result"] = data2

            data2 = st.session_state.get("ab_result")
            if data2:
                bm = data2.get("b_medium_image_urls") or []
                ba = data2.get("b_aggressive_image_urls") or []
                n = max(len(bm), len(ba))

                st.caption(f"共生成 {n} 张（与原稿插图数量一致），B 提供 中等/激进 对比（像素级保真）")
                for i in range(n):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.caption(f"B-中等 #{i+1}")
                        if i < len(bm):
                            full = f"{api_url.replace('/api/v1','')}{bm[i]}"
                            data = _fetch_bytes(full)
                            if data:
                                st.image(data, use_container_width=True)
                            else:
                                st.caption("图片加载失败")
                    with col2:
                        st.caption(f"B-激进 #{i+1}")
                        if i < len(ba):
                            full = f"{api_url.replace('/api/v1','')}{ba[i]}"
                            data = _fetch_bytes(full)
                            if data:
                                st.image(data, use_container_width=True)
                            else:
                                st.caption("图片加载失败")

                if data2.get("b_error"):
                    st.warning(f"B 有自动回退/失败项（不影响整体出图）：{data2['b_error']}")
                st.caption(f"产物目录: {data2.get('artifacts_dir')}")


# (Removed) 文案仿写页面：当前只保留洗稿功能
