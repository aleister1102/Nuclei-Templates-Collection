#!/usr/bin/env python3
import os
import sys
import json
import time
import subprocess
import requests
import re
from datetime import datetime

# --- CẤU HÌNH ---
class Config:
    REPO_LIST_FILE = "README.txt"
    CLONE_DIR = "community-templates"
    TOP_N_REPOS = 50
    MARKDOWN_RESULT_FILE = "filtered.md"
    API_CACHE_FILE = "api_cache.json"
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
    # Giới hạn kích thước repo (tính bằng KB). 5MB = 5 * 1024 = 5120 KB
    MAX_REPO_SIZE_KB = 5120

# --- CÁC HÀM LOGIC ---

def parse_markdown_for_repos(md_file):
    """Đọc file markdown kết quả và trích xuất danh sách URL repo."""
    print(f"📄 Tìm thấy file '{md_file}'. Đang sử dụng danh sách repo từ file này...")
    repos = []
    try:
        with open(md_file, 'r') as f:
            for line in f:
                match = re.search(r"\[(https://github.com/.+?)\]", line)
                if match:
                    repos.append({"url": match.group(1)})
    except FileNotFoundError:
        return []
    
    if not repos:
        print(f"⚠️ Không thể trích xuất repo nào từ '{md_file}'. Sẽ tiến hành gọi API.", file=sys.stderr)
    return repos

def get_top_repos_from_api(config):
    """
    Lấy danh sách repo hàng đầu bằng cách gọi API, lọc theo kích thước, sử dụng cache và xử lý rate limit.
    """
    print("🔎 Không tìm thấy file kết quả. Bắt đầu quá trình lấy dữ liệu từ API GitHub...")
    
    cache = _load_cache(config.API_CACHE_FILE)
    
    try:
        with open(config.REPO_LIST_FILE, 'r') as f:
            urls = list(set(line.strip() for line in f if "github.com" in line.strip()))
    except FileNotFoundError:
        print(f"❌ Lỗi: Không tìm thấy file '{config.REPO_LIST_FILE}'.", file=sys.stderr)
        sys.exit(1)

    all_repos_data = []
    for url in urls:
        repo_info = _fetch_repo_metadata(url, cache, config)
        if repo_info:
            all_repos_data.append(repo_info)
    
    _save_cache(config.API_CACHE_FILE, cache)
    
    all_repos_data.sort(key=lambda x: x['stars'], reverse=True)
    top_repos = all_repos_data[:config.TOP_N_REPOS]
    
    _write_markdown_file(config.MARKDOWN_RESULT_FILE, top_repos)
    return top_repos

def clone_or_update_repos(config, repos_to_process):
    """Clone hoặc cập nhật các kho chứa được chỉ định."""
    if not repos_to_process:
        print("🤷 Không có kho chứa nào để xử lý. Dừng lại.")
        return
        
    print(f"\n🚀 Sẽ tiến hành clone/cập nhật {len(repos_to_process)} kho chứa...")
    os.makedirs(config.CLONE_DIR, exist_ok=True)
    
    for repo in repos_to_process:
        try:
            parts = repo['url'].strip("/").split('/')
            owner, repo_name = parts[-2], parts[-1].replace(".git", "")
            target_dir = os.path.join(config.CLONE_DIR, f"{owner}__{repo_name}".lower())

            if os.path.isdir(target_dir):
                print(f"🔄 Đang cập nhật {repo_name}...")
                subprocess.run(["git", "-C", target_dir, "pull"], check=True, capture_output=True)
            else:
                stars_info = f"(⭐ {repo.get('stars', 'N/A')})" if 'stars' in repo else ""
                print(f"📥 Đang clone {repo_name} {stars_info}...")
                subprocess.run(["git", "clone", repo['url'], target_dir], check=True, capture_output=True)

        except (subprocess.CalledProcessError, IndexError, KeyError) as e:
            error_message = e.stderr.decode() if isinstance(e, subprocess.CalledProcessError) else str(e)
            print(f"❌ Lỗi khi xử lý {repo.get('url', 'URL không xác định')}: {error_message}", file=sys.stderr)

# --- CÁC HÀM HỖ TRỢ (Private) ---

def _load_cache(cache_file):
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            return json.load(f)
    return {}

def _save_cache(cache_file, cache_data):
    with open(cache_file, 'w') as f:
        json.dump(cache_data, f, indent=4)

def _fetch_repo_metadata(repo_url, cache, config):
    """Hàm lấy metadata của repo, bao gồm cả kiểm tra kích thước."""
    if repo_url in cache and 'size' in cache[repo_url]: # Kiểm tra xem cache cũ có 'size' không
        # Nếu repo trong cache đã bị lọc vì kích thước, thì bỏ qua luôn
        if cache[repo_url] is None: return None
        # Kiểm tra lại size trong cache với config hiện tại
        if cache[repo_url]['size'] > config.MAX_REPO_SIZE_KB:
             print(f"📦 Bỏ qua từ cache (quá lớn): {repo_url} ({cache[repo_url]['size']} KB)")
             return None
        print(f"📦 Dùng cache cho: {repo_url}")
        return cache[repo_url]

    api_url = f"https://api.github.com/repos/{'/'.join(repo_url.strip('/').split('/')[-2:]).replace('.git','')}"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"token {config.GITHUB_TOKEN}"

    try:
        response = requests.get(api_url, headers=headers)
        if response.status_code in [404, 403, 451]: return None
        if response.status_code == 429 or ('X-RateLimit-Remaining' in response.headers and int(response.headers['X-RateLimit-Remaining']) == 0):
            reset_time = int(response.headers.get('X-RateLimit-Reset', time.time() + 60))
            wait_time = max(reset_time - time.time(), 0) + 1
            print(f"⏳ Bị giới hạn API. Đang đợi {int(wait_time)} giây...", file=sys.stderr)
            time.sleep(wait_time)
            return _fetch_repo_metadata(repo_url, cache, config) # Thử lại
        
        response.raise_for_status()
        data = response.json()
        
        repo_size_kb = data.get("size", 0)
        # *** LOGIC LỌC KÍCH THƯỚC ***
        if repo_size_kb > config.MAX_REPO_SIZE_KB:
            print(f"🚫 Bỏ qua (quá lớn): {repo_url} ({repo_size_kb} KB > {config.MAX_REPO_SIZE_KB} KB)")
            cache[repo_url] = None # Lưu "None" vào cache để không gọi lại repo này
            return None

        result = {
            "url": repo_url, 
            "stars": data.get("stargazers_count", 0),
            "size": repo_size_kb
        }
        cache[repo_url] = result
        print(f"📞 Lấy từ API: {repo_url} - ⭐ {result['stars']} - {result['size']} KB")
        return result
    except requests.exceptions.RequestException:
        return None

def _write_markdown_file(md_file, repos):
    """Ghi kết quả ra file markdown, thêm cột kích thước."""
    with open(md_file, 'w') as f:
        f.write(f"# Top {len(repos)} Kho Chứa Git (Nhỏ hơn {Config.MAX_REPO_SIZE_KB / 1024}MB)\n\n")
        f.write(f"*Tự động tạo lúc: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")
        f.write("*Xóa file này để buộc kịch bản lấy lại dữ liệu mới từ API.*\n\n")
        f.write("| Hạng | Tên Kho Chứa | ⭐ Sao | Kích thước | URL |\n")
        f.write("|------|--------------|-------|------------|-----|\n")
        for i, repo in enumerate(repos, 1):
            owner, repo_name = repo['url'].strip("/").split('/')[-2:]
            repo_name = repo_name.replace('.git','')
            size_mb = f"{repo['size'] / 1024:.2f} MB"
            f.write(f"| {i} | `{owner}/{repo_name}` | {repo['stars']} | `{size_mb}` | [{repo['url']}]({repo['url']}) |\n")
    print(f"\n✅ Đã lưu kết quả xếp hạng vào file: {md_file}")

# --- ĐIỂM KHỞI ĐỘNG ---

def main():
    """Hàm điều phối chính của kịch bản."""
    config = Config()
    
    # Logic không đổi: ưu tiên dùng markdown, nếu không thì gọi API
    repos_to_process = parse_markdown_for_repos(config.MARKDOWN_RESULT_FILE)
    if not repos_to_process:
        repos_to_process = get_top_repos_from_api(config)
    
    clone_or_update_repos(config, repos_to_process)
    
    print("\n🎉 Hoàn thành!")

if __name__ == "__main__":
    main()
