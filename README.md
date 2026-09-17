# Hitomi 多线程下载器

一个基于 Python 标准库的 Windows 图形下载器。输入图库页面链接后，程序会读取页面元数据，并发下载图片，最后打包为 ZIP 文件。

## 功能

- Tkinter 图形界面
- 支持 1 到 32 路并发
- 实时显示下载进度
- 自定义输出目录
- 下载完成后自动打包 ZIP
- 失败自动重试，并尝试备用 CDN 节点
- 同时保留命令行用法

## 使用源码运行

需要 Python 3.10 或更高版本。程序只使用 Python 标准库，不需要安装第三方依赖。

双击或运行下面的命令启动图形界面：

```powershell
python downloader.py
```

也可以使用命令行：

```powershell
python downloader.py "https://hitomi.la/imageset/example-123456.html#1"
```

指定并发数和输出文件：

```powershell
python downloader.py "https://hitomi.la/imageset/example-123456.html#1" --workers 16 --output "D:\Downloads\book.zip"
```

并发数最大为 32。通常建议使用 8 到 16，过高可能导致 CDN 限速或失败。

## 免责声明

本项目仅提供通用的页面资源下载和本地打包功能。使用者应当只下载自己有权保存的内容，并遵守目标网站的服务条款、版权规定及所在地法律法规。项目作者不对使用者下载或传播的内容负责。

## License

本项目采用 MIT License，详见 [LICENSE](LICENSE)。
