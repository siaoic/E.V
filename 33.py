import os
import sys
from pathlib import Path

def search_text_in_files(directory, search_text, extensions=None, exclude_extensions=None, exclude_dirs=None):
    """
    遍历目录下所有文件，搜索包含指定文本的文件
    
    参数:
        directory: 要搜索的目录路径
        search_text: 要搜索的文本
        extensions: 限定搜索的文件扩展名列表，如 ['.txt', '.py']，None表示搜索所有文件
        exclude_extensions: 排除的文件扩展名列表
        exclude_dirs: 排除的目录名称列表，如 ['runtime', 'node_modules']
    """
    found_files = []
    total_files = 0
    total_searched = 0
    
    # 转换为Path对象
    root_path = Path(directory)
    
    if not root_path.exists():
        print(f"错误：目录 '{directory}' 不存在")
        return found_files
    
    if not root_path.is_dir():
        print(f"错误：'{directory}' 不是一个目录")
        return found_files
    
    # 默认排除常见目录
    if exclude_dirs is None:
        exclude_dirs = ['runtime', 'node_modules', '.git', '__pycache__', 'venv', 'env', 'dist', 'build']
    
    print(f"开始搜索目录: {root_path.absolute()}")
    print(f"搜索文本: '{search_text}'")
    if extensions:
        print(f"文件扩展名过滤: {', '.join(extensions)}")
    if exclude_dirs:
        print(f"排除目录: {', '.join(exclude_dirs)}")
    print("-" * 60)
    
    # 遍历目录
    for item in root_path.rglob('*'):
        if not item.is_file():
            continue
            
        total_files += 1
        
        # 检查路径中是否包含要排除的目录
        should_exclude = False
        for exclude_dir in exclude_dirs:
            # 检查文件路径的每个部分
            if exclude_dir in item.parts:
                should_exclude = True
                break
        
        if should_exclude:
            continue
        
        # 检查文件扩展名
        if extensions:
            if item.suffix.lower() not in extensions:
                continue
                
        if exclude_extensions:
            if item.suffix.lower() in exclude_extensions:
                continue
        
        total_searched += 1
        
        # 尝试读取文件并搜索文本
        try:
            # 对于大文件，逐行读取
            found_any = False
            match_lines = []
            with open(item, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    if search_text.lower() in line.lower():
                        if not found_any:
                            print(f"\n✓ 找到: {item}")
                            print(f"  路径: {item.absolute()}")
                            found_files.append(str(item))
                            found_any = True
                        match_lines.append((line_num, line.strip()))
                        if len(match_lines) >= 3:  # 最多显示3行匹配内容
                            break
            
            # 显示匹配的行
            if found_any:
                for line_num, line in match_lines[:3]:
                    # 截断过长的行
                    if len(line) > 100:
                        line = line[:100] + "..."
                    print(f"    行 {line_num}: {line}")
                        
        except (UnicodeDecodeError, PermissionError, OSError) as e:
            # 跳过无法读取的文件（二进制文件、权限问题等）
            continue
    
    # 打印总结
    print("\n" + "=" * 60)
    print(f"搜索完成！")
    print(f"总文件数: {total_files}")
    print(f"已搜索文件数: {total_searched}")
    print(f"找到包含 '{search_text}' 的文件数: {len(found_files)}")
    
    if found_files:
        print("\n包含搜索文本的文件列表:")
        for f in found_files:
            print(f"  - {f}")
    
    return found_files


def main():
    """主函数 - 示例用法"""
    # 方式1: 从命令行参数获取
    if len(sys.argv) >= 2:
        search_directory = sys.argv[1]
    else:
        search_directory = input("请输入要搜索的目录路径 (默认当前目录): ").strip()
        if not search_directory:
            search_directory = "."
    
    if len(sys.argv) >= 3:
        search_text = sys.argv[2]
    else:
        search_text = input("请输入要搜索的文本 (默认 'zhipu'): ").strip()
        if not search_text:
            search_text = "zhipu"
    
    # 设置要排除的目录
    print("\n默认排除目录: runtime, node_modules, .git, __pycache__, venv, env, dist, build")
    custom_exclude = input("是否要自定义排除目录？(y/n, 默认n): ").strip().lower()
    
    exclude_dirs = ['runtime', 'node_modules', '.git', '__pycache__', 'venv', 'env', 'dist', 'build']
    if custom_exclude == 'y':
        exclude_input = input("请输入要排除的目录名（用逗号分隔，如：runtime,logs,temp）: ")
        if exclude_input.strip():
            exclude_dirs = [d.strip() for d in exclude_input.split(',') if d.strip()]
    
    # 可选：限定文件类型
    print("\n是否要限定搜索的文件类型？")
    print("1. 所有文件")
    print("2. 仅文本文件 (.txt, .md, .log)")
    print("3. 代码文件 (.py, .java, .cpp, .js, .html, .css)")
    print("4. 自定义扩展名")
    print("5. 跳过二进制文件（常见格式）")
    
    choice = input("请选择 (1-5, 默认1): ").strip()
    
    extensions = None
    exclude_extensions = None
    
    if choice == "2":
        extensions = ['.txt', '.md', '.rst', '.log', '.csv', '.json', '.xml', '.yaml', '.yml']
    elif choice == "3":
        extensions = ['.py', '.java', '.cpp', '.c', '.h', '.js', '.jsx', '.ts', '.tsx', 
                     '.html', '.htm', '.css', '.scss', '.less', '.xml', '.json', 
                     '.yaml', '.yml', '.sh', '.bash']
    elif choice == "4":
        ext_input = input("请输入文件扩展名（用逗号分隔，如：.txt,.py）: ")
        extensions = [ext.strip().lower() for ext in ext_input.split(',') if ext.strip()]
    elif choice == "5":
        # 跳过常见二进制文件
        exclude_extensions = ['.exe', '.dll', '.so', '.dylib', '.pyc', '.pyo', 
                             '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.ico',
                             '.mp3', '.mp4', '.avi', '.mov', '.wmv',
                             '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2',
                             '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx']
    
    # 执行搜索
    search_text_in_files(search_directory, search_text, extensions, exclude_extensions, exclude_dirs)


if __name__ == "__main__":
    main()