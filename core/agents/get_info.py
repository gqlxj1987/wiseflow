# -*- coding: utf-8 -*-
import asyncio
from loguru import logger
import os, re
from llms.openai_wrapper import openai_llm as llm
# from core.llms.siliconflow_wrapper import sfa_llm # or other llm wrapper
from utils.general_utils import normalize_url, url_pattern
from .get_info_prompts import *
from .constants import common_file_exts, common_tlds


async def _clean_raw_markdown(raw_markdown: str) -> str:
    """Cleans the raw markdown string by removing special URL formats."""
    # for special url formate from craw4ai-de 0.4.247
    return re.sub(r'<javascript:.*?>', '<javascript:>', raw_markdown).strip()


async def _convert_image_markdown(raw_markdown: str) -> str:
    """Converts image markdown (![alt](src)) to a custom format (§alt||src§)."""
    # 处理图片标记 ![alt](src)，使用非贪婪匹配并考虑嵌套括号的情况
    i_pattern = r'(!\[(.*?)\]\(((?:[^()]*|\([^()]*\))*)\))'
    matches = re.findall(i_pattern, raw_markdown, re.DOTALL)
    for _sec, alt, src in matches:
        # 替换为新格式 §alt||src§
        raw_markdown = raw_markdown.replace(_sec, f'§{alt}||{src}§', 1)
    return raw_markdown


async def pre_process(raw_markdown: str, base_url: str, used_img: list[str], 
                        recognized_img_cache: dict, existing_urls: set = set(), 
                        test_mode: bool = False) -> tuple[dict, list[str], list[str], dict]:

    link_dict = {}

    raw_markdown = await _clean_raw_markdown(raw_markdown)
    raw_markdown = await _convert_image_markdown(raw_markdown)


async def _process_markdown_links(text: str, base_url: str, link_dict: dict, 
                                  recognized_img_cache: dict, existing_urls: set,
                                  used_img: list[str] # Added used_img
                                  ) -> tuple[str, int, int]:
    """Processes markdown links `[text](url)`, extracts URLs, and handles nested images."""
    score = 0
    _valid_len = len(text.strip())
    link_pattern = r'(\[(.*?)\]\(((?:[^()]*|\([^()]*\))*)\))'
    matches = re.findall(link_pattern, text, re.DOTALL)

    for _sec, link_text, link_url in matches:
        _title = re.sub(url_pattern, '', link_url, re.DOTALL).strip().strip('"')
        link_text = link_text.strip()
        if _title and _title not in link_text:
            link_text = f"{_title} - {link_text}"

        _url = re.findall(url_pattern, link_url)
        if not _url or _url[0].startswith(('#', 'javascript:')):
            text = text.replace(_sec, link_text, 1)
            continue
        
        score += 1
        _valid_len -= len(_sec)
        url = normalize_url(_url[0], base_url)

        img_marker_pattern = r'§(.*?)\|\|(.*?)§'
        inner_matches = re.findall(img_marker_pattern, link_text, re.DOTALL)
        for alt, src in inner_matches:
            link_text = link_text.replace(f'§{alt}||{src}§', '')

        if not link_text and inner_matches: # Image is the link content
            img_alt = inner_matches[0][0].strip()
            img_src_raw = inner_matches[0][1].strip()
            if img_src_raw and not img_src_raw.startswith('#'):
                img_src = normalize_url(img_src_raw, base_url)
                if not img_src:
                    link_text = img_alt
                # Condition to use alt text or recognize image
                elif len(img_alt) > 2 or url in existing_urls or \
                     any(img_src.endswith(tld) or img_src.endswith(tld + '/') for tld in common_tlds) or \
                     any(img_src.endswith(ext) for ext in common_file_exts if ext not in ['jpg', 'jpeg', 'png']):
                    _key = f"[img{len(link_dict)+1}]"
                    link_dict[_key] = img_src
                    link_text = img_alt
                else: # Recognize image
                    if img_src not in recognized_img_cache:
                        recognized_img_cache[img_src] = await extract_info_from_img(img_src)
                    _key = f"[img{len(link_dict)+1}]"
                    link_dict[_key] = img_src
                    link_text = recognized_img_cache[img_src]
            else: # No valid image src
                link_text = img_alt
        
        _key = f"[{len(link_dict)+1}]"
        link_dict[_key] = url
        text = text.replace(_sec, link_text + _key, 1)
    return text, score, _valid_len


async def _process_standalone_images(text: str, base_url: str, link_dict: dict,
                                     recognized_img_cache: dict, used_img: list[str]
                                     ) -> str:
    """Processes standalone image tags §alt||src§, normalizes URLs, and updates link_dict."""
    img_pattern = r'(§(.*?)\|\|(.*?)§)'
    matches = re.findall(img_pattern, text, re.DOTALL)
    remained_text = re.sub(img_pattern, '', text, re.DOTALL).strip()
    remained_text_len = len(remained_text)

    for _sec, alt, src_raw in matches:
        alt = alt.strip()
        src_raw = src_raw.strip()

        if not src_raw or src_raw.startswith('#') or src_raw not in used_img:
            text = text.replace(_sec, alt, 1)
            continue

        img_src = normalize_url(src_raw, base_url)
        if not img_src:
            text = text.replace(_sec, alt, 1)
        # Condition to use alt text or recognize image
        elif remained_text_len > 5 or len(alt) > 2 or \
             any(img_src.endswith(tld) or img_src.endswith(tld + '/') for tld in common_tlds) or \
             any(img_src.endswith(ext) for ext in common_file_exts if ext not in ['jpg', 'jpeg', 'png']):
            _key = f"[{len(link_dict)+1}]" # Use generic key as it might be a document/other non-image file
            link_dict[_key] = img_src
            text = text.replace(_sec, alt + _key, 1)
        else: # Recognize image
            if img_src not in recognized_img_cache:
                recognized_img_cache[img_src] = await extract_info_from_img(img_src)
            _key = f"[img{len(link_dict)+1}]" # Specific key for recognized images
            link_dict[_key] = img_src
            text = text.replace(_sec, recognized_img_cache[img_src] + _key, 1)
    return text


async def check_url_text(text) -> tuple[int, str]:
        score = 0
        _valid_len = len(text.strip())

        # 找到所有[part0](part1)格式的片段，使用非贪婪匹配并考虑嵌套括号的情况
        # This section will be replaced by calling _process_markdown_links
        text, md_link_score, md_link_valid_len_reduction = await _process_markdown_links(
            text, base_url, link_dict, recognized_img_cache, existing_urls, used_img
        )
        score += md_link_score
        _valid_len -= (len(text.strip()) - md_link_valid_len_reduction) # approximate reduction

        # 处理文本中的其他图片标记
        text = await _process_standalone_images(text, base_url, link_dict, recognized_img_cache, used_img)

        # 处理文本中的"野 url"，使用更精确的正则表达式
        # This section will be replaced by calling _process_bare_urls
        text, bare_url_score, bare_url_valid_len_reduction = await _process_bare_urls(
            text, base_url, link_dict
        )
        score += bare_url_score
        _valid_len -= bare_url_valid_len_reduction
        
        if score == 0:
            # 如果没有任何链接，则认为这是一段纯文本
            return 999, text
        # 统计换行符数量
        newline_count = text.count(' * ')
        score += newline_count
        ratio = _valid_len/score if score != 0 else 999

        return ratio, text


async def _process_bare_urls(text: str, base_url: str, link_dict: dict) -> tuple[str, int, int]:
    """Processes bare URLs in the text, normalizes them, and updates link_dict."""
    score = 0
    _valid_len_reduction = 0
    matches = re.findall(url_pattern, text)
    for url_match in matches:
        # url_match could be a tuple if the regex has multiple groups, ensure to get the actual URL string
        actual_url = url_match if isinstance(url_match, str) else url_match[0]
        normalized = normalize_url(actual_url, base_url)
        if normalized: # Ensure normalized URL is not empty
            _key = f"[{len(link_dict)+1}]"
            link_dict[_key] = normalized
            text = text.replace(actual_url, _key, 1) # Replace original URL string
            score += 1
            _valid_len_reduction += len(actual_url)
    return text, score, _valid_len_reduction


def _remove_navigation_sections(sections: list[str], test_mode: bool) -> list[str]:
    """Removes potential navigation/header sections from the list of sections."""
    if len(sections) > 2: # Only apply if there are enough sections to have a header/footer
        _sec = sections[0]
        # 更新正则表达式以处理嵌套括号
        section_remain = re.sub(r'\[(.*?)\]\(((?:[^()]*|\([^()]*\))*)\)', '', _sec, re.DOTALL).strip()
        section_remain_len = len(section_remain)
        # 更新正则表达式以处理嵌套括号
        total_links = len(re.findall(r'\[(.*?)\]\(((?:[^()]*|\([^()]*\))*)\)', _sec, re.DOTALL))
        ratio = total_links / section_remain_len if section_remain_len != 0 else 1
        if ratio > 0.05:
            if test_mode:
                print('\033[31mthis is a navigation section, will be removed\033[0m')
                print(ratio, '\n')
                print(section_remain)
                print('-' * 50)
            return sections[1:]
    return sections


def _remove_footer_sections(sections: list[str], test_mode: bool) -> list[str]:
    """Removes potential footer sections from the list of sections."""
    if len(sections) > 2: # Only apply if there are enough sections to have a header/footer
        _sec = sections[-1]
        # 更新正则表达式以处理嵌套括号
        section_remain = re.sub(r'\[(.*?)\]\(((?:[^()]*|\([^()]*\))*)\)', '', _sec, re.DOTALL).strip()
        section_remain_len = len(section_remain)
        if section_remain_len < 198: # Threshold for footer
            if test_mode:
                print('\033[31mthis is a footer section, will be removed\n\033[0m')
                print(section_remain_len)
                print(section_remain)
                print('-' * 50)
            return sections[:-1]
    return sections


    sections = raw_markdown.split('# ') # use '# ' to avoid # in url
    sections = _remove_navigation_sections(sections, test_mode)
    sections = _remove_footer_sections(sections, test_mode)

    # The check_url_text logic is now part of _separate_content_and_links
    # We need to pass all necessary parameters to it.
    links_parts, contents = await _separate_content_and_links(
        sections, base_url, link_dict, recognized_img_cache, existing_urls, used_img, test_mode
    )

    return link_dict, links_parts, contents, recognized_img_cache


async def _separate_content_and_links(sections: list[str], base_url: str, link_dict: dict,
                                      recognized_img_cache: dict, existing_urls: set,
                                      used_img: list[str], test_mode: bool
                                      ) -> tuple[list[str], list[str]]:
    """
    Separates text sections into link-heavy parts and content-heavy parts.
    Processes links and images within each section.
    """
    links_parts = []
    contents = []

    async def _check_url_text_for_separation(text_section: str) -> tuple[int, str]:
        # This inner function re-integrates the logic from the original check_url_text,
        # but it's now specifically for the separation task.
        # Parameters like base_url, link_dict, etc., are available from the outer scope.
        current_score = 0
        current_valid_len = len(text_section.strip())

        processed_text, md_link_score, md_link_valid_len_reduction = await _process_markdown_links(
            text_section, base_url, link_dict, recognized_img_cache, existing_urls, used_img
        )
        current_score += md_link_score
        # Adjust current_valid_len based on the reduction from _process_markdown_links
        # The original calculation was: _valid_len -= (len(text.strip()) - md_link_valid_len_reduction)
        # This seems a bit off. A direct subtraction of (original_len - new_len) of text processed by link processor
        # might be more accurate if _process_markdown_links returns the length reduction directly.
        # For now, let's assume md_link_valid_len_reduction is the amount of non-link text removed/replaced.
        # A simpler way: current_valid_len is the length of text *before* this processing step.
        # The reduction should be the length of the link sections that were replaced.
        # The returned _valid_len from _process_markdown_links was meant to be the new valid length.
        # Let's re-evaluate: _valid_len was initialized with len(text.strip()).
        # Each time a link _sec was processed, _valid_len was reduced by len(_sec).
        # So md_link_valid_len_reduction should be the sum of len(_sec) for processed links.
        # The _valid_len returned by _process_markdown_links is the original length MINUS the length of link placeholders.
        # This means current_valid_len should be updated to the value returned by _process_markdown_links if it reflects remaining text length.
        # Let's stick to the original logic for _valid_len calculation as closely as possible.
        # The `md_link_valid_len_reduction` (which is the returned `_valid_len` from `_process_markdown_links`)
        # represents the length of the text after link placeholders have been substituted, but only considering the original text processed by it.
        # It's the length of the text that is *not* part of a markdown link.
        current_valid_len = md_link_valid_len_reduction # This should be the text length excluding markdown links

        processed_text = await _process_standalone_images(
            processed_text, base_url, link_dict, recognized_img_cache, used_img
        )
        
        processed_text, bare_url_score, bare_url_valid_len_reduction = await _process_bare_urls(
            processed_text, base_url, link_dict
        )
        current_score += bare_url_score
        current_valid_len -= bare_url_valid_len_reduction
        
        if current_score == 0:
            return 999, processed_text # Pure text
            
        newline_count = processed_text.count(' * ') # TODO: ' * ' seems specific, is it a typo for '\n'? Assuming it's intentional.
        current_score += newline_count
        # Ensure current_valid_len is not negative after subtractions
        current_valid_len = max(0, current_valid_len)
        ratio = current_valid_len / current_score if current_score != 0 else 999
        return ratio, processed_text

    for section_text in sections:
        ratio, processed_section_text = await _check_url_text_for_separation(section_text)
        
        if ratio < 90: # Threshold for link-heavy part
            if test_mode:
                print('\033[32mthis is a links part\033[0m')
                print(ratio, '\n')
                print(processed_section_text)
                print('-' * 50)
            # Handle large text splitting
            if len(processed_section_text) > 30000:
                lines = processed_section_text.split('\n')
                _text_buffer = ''
                while lines:
                    line = lines.pop(0)
                    _text_buffer = f'{_text_buffer}{line}\n'
                    if len(_text_buffer) > 29000 or not lines:
                        links_parts.append(_text_buffer)
                        _text_buffer = ''
            else:
                links_parts.append(processed_section_text)
        else: # Content-heavy part
            if test_mode:
                print('\033[34mthis is a content part\033[0m')
                print(ratio, '\n')
                print(processed_section_text)
                print('-' * 50)
            # Handle large text splitting
            if len(processed_section_text) > 30000:
                lines = processed_section_text.split('\n')
                _text_buffer = ''
                while lines:
                    line = lines.pop(0)
                    _text_buffer = f'{_text_buffer}{line}\n'
                    if len(_text_buffer) > 29000 or not lines:
                        contents.append(_text_buffer)
                        _text_buffer = ''
            else:
                contents.append(processed_section_text)
                
    return links_parts, contents


vl_model = os.environ.get("VL_MODEL", "")
if not vl_model:
    print("VL_MODEL not set, will skip extracting info from img, some info may be lost!")


async def extract_info_from_img(url: str) -> str:
    if not vl_model:
        return '§to_be_recognized_by_visual_llm§'

    llm_output = await llm([{"role": "user",
        "content": [{"type": "image_url", "image_url": {"url": url, "detail": "high"}},
        {"type": "text", "text": "提取图片中的所有文字，如果图片不包含文字或者文字很少或者你判断图片仅是网站logo、商标、图标等，则输出NA。注意请仅输出提取出的文字，不要输出别的任何内容。"}]}],
        model=vl_model)

    return llm_output.strip() if llm_output else llm_output


async def get_author_and_publish_date(text: str, model: str, test_mode: bool = False, _logger: logger = None) -> tuple[str, str]:
    if not text:
        return "", ""

    if len(text) > 2048:
        text = f'{text[:2048]}......'

    content = f'<text>\n{text}\n</text>\n\n{get_ap_suffix}'
    result = await llm([{'role': 'system', 'content': get_ap_system}, {'role': 'user', 'content': content}],
                            model=model, temperature=0.1)
                     
    if test_mode:
        print(f"llm output:\n {result}")
        
    author = re.findall(r'<source>(.*?)</source>', result, re.DOTALL)
    publish_date = re.findall(r'<publish_date>(.*?)</publish_date>', result, re.DOTALL)

    author = author[-1] if author else ''
    publish_date = publish_date[-1] if publish_date else ''

    if not author or not publish_date:
        if _logger:
            _logger.warning(f"failed to parse from llm output: {result}")

    return author if author.lower() != 'na' else '', publish_date


async def get_more_related_urls(texts: list[str], link_dict: dict, prompts: list[str], test_mode: bool = False,
                                _logger: logger = None) -> set:
    
    sys_prompt, suffix, model = prompts
    text_batch = ''
    cache = set()
    while texts:
        t = texts.pop(0)
        text_batch = f'{text_batch}{t}\n\n'
        if len(text_batch) > 2048 or len(texts) == 0:
            content = f'<text>\n{text_batch}</text>\n\n{suffix}'
            result = await llm(
                    [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': content}],
                    model=model, temperature=0.1)

            if test_mode:
                print(f"llm output:\n {result}")

            answer_list = re.findall(r'<answer>(.*?)</answer>', result, re.DOTALL)
            if answer_list:
                if len(answer_list) > 1 and _logger:
                    _logger.warning(f"LLM returned multiple <answer> tags in get_more_related_urls. Using the last one. Output: {result}")
                processed_answer = answer_list[-1] # Use the content of the last <answer> tag
                links = re.findall(r'\[\d+]', processed_answer)
                for link in links:
                    if link not in link_dict or link not in text_batch:
                        if _logger:
                            _logger.warning(f"model generating hallucination:\n{link}\n{result[-1]}\n{text_batch}")
                        if test_mode:
                            print(f"model hallucination:\n{link}\n{result[-1]}\n{text_batch}")
                        continue
                    cache.add(link)
            text_batch = ''

    more_urls = set()
    for mark in cache:
        url = link_dict[mark]
        has_common_ext = any(url.endswith(ext) for ext in common_file_exts)
        has_common_tld = any(url.endswith(tld) or url.endswith(tld + '/') for tld in common_tlds)
        if has_common_ext or has_common_tld:
            continue
        more_urls.add(url)
    
    return more_urls
    

async def get_info(texts: list[str], link_dict: dict, prompts: list[str], author: str, publish_date: str,
                   test_mode: bool = False, _logger: logger = None) -> list[dict]:

    sys_prompt, suffix, model = prompts

    if test_mode:
        info_pre_fix = ''
    else:
        info_pre_fix = f"//{author} {publish_date}//"
    
    texts = [t for t in texts if t.strip()]
    if not texts:
        return []

    batches = []
    text_batch = f'Author: {author}\nPublish Date: {publish_date}\n'
    while texts:
        t = texts.pop(0)
        text_batch = f'{text_batch}{t}# '
        if len(text_batch) > 9999 or len(texts) == 0:
            content = f'<text>\n{text_batch}</text>\n\n{suffix}'
            batches.append(content)
            text_batch = f'Author: {author}\nPublish Date: {publish_date}\n'

    tasks = [
        llm([{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': content}], model=model, temperature=0.1)
        for content in batches]
    results = await asyncio.gather(*tasks)

    final = []
    for res in results:
        if test_mode:
            print(f"llm output:\n {res}")
        summary_list = re.findall(r'<summary>(.*?)</summary>', res, re.DOTALL)
        if not summary_list:
            if _logger:
                _logger.warning("model lightly hallucination: contains no summary tag")
            if test_mode:
                print("model lightly hallucination: contains no summary tag")
            continue
        
        if len(summary_list) > 1 and _logger:
            _logger.warning(f"LLM returned multiple <summary> tags in get_info. Using the last one. Output: {res}")
        
        processed_summary = summary_list[-1].strip()
        if _logger:
            _logger.debug(processed_summary)
        if test_mode:
            print(processed_summary)
        if len(processed_summary) < 3: # Handles "NA" or very short summaries
            continue

        url_tags = re.findall(r'\[\d+]', processed_summary)
        refences = {}
        for _tag in url_tags:
            if _tag in link_dict:
                refences[_tag] = link_dict[_tag]
            else:
                if _logger and link_dict: # avoid warning when link_dict is empty(search engine)
                    _logger.warning(f"model hallucination: {res} \ncontains {_tag} which is not in link_dict")
                if test_mode:
                    print(f"model hallucination: {res} \ncontains {_tag} which is not in link_dict")
                res = res.replace(_tag, '')
        final.append({'content': f"{info_pre_fix}{res}", 'references': refences})
    
    return final
