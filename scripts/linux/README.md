# Linux 打包：内置 libmpv

Linux 版 media_kit 不随应用提供 libmpv，插件在运行时会查找系统的
`libmpv.so.2`。SteamOS（Steam Deck）是不可变系统，默认没有 libmpv，
也没有 libxml2，直接解压官方 zip 会报：

```
error while loading shared libraries: libmpv.so.2: cannot open shared object file
```

这两个脚本把 libmpv 及其运行库打进 Flutter bundle，使 zip/deb 不依赖系统
安装的 mpv，且不怕 SteamOS 系统更新。

## 脚本

- `fetch_libmpv_runtime.py`
  从 Debian 12 (bookworm) 下载 libmpv2 及其完整依赖闭包，解包、裁剪后写入
  `third_party/libmpv-linux/runtime/`（已加入 `.gitignore`），并为每个库设置
  `$ORIGIN` RUNPATH。Debian 12 的库只要求 glibc >= 2.34，兼容所有现役
  SteamOS 和主流桌面发行版。

  依赖：`python3`、`dpkg-deb`、`patchelf`。

  ```bash
  sudo apt install patchelf dpkg
  python3 scripts/linux/fetch_libmpv_runtime.py
  ```

  国内网络可指定镜像（默认 `deb.debian.org`）：

  ```bash
  DEBIAN_MIRROR=https://mirrors.aliyun.com/debian python3 scripts/linux/fetch_libmpv_runtime.py
  ```

  结果会缓存：脚本或依赖集合不变时再次运行直接跳过；需要强制重建时设置
  `LIBMPV_FORCE=1`。

- `inject_libmpv_runtime.sh`
  把上述运行库复制到 Flutter bundle 的 `lib/`，并把
  `libmedia_kit_video_plugin.so` 的 RUNPATH 改为 `$ORIGIN`，使其加载包内
  libmpv。

  ```bash
  scripts/linux/inject_libmpv_runtime.sh simple_live_app/build/linux/x64/release/bundle
  ```

## CI

`.github/workflows/publish_app_release_linux.yml` 和
`publish_app_dev.yaml` 已经接好：

1. 安装 `patchelf`，用 `actions/cache` 缓存运行时目录；
2. 构建前执行 `fetch_libmpv_runtime.py`；
3. `flutter_distributor` 使用 `--hook-pre` 调用注入脚本——钩子在
   `flutter build` 之后、打包 deb/zip 之前执行，因此两个产物都自带运行库。

本地用 `flutter_distributor` 打包时，同样加上钩子即可：

```bash
cd simple_live_app
flutter_distributor package --platform linux --targets deb,zip --skip-clean \
  --hook-pre 'bash "$PWD/../scripts/linux/inject_libmpv_runtime.sh" "$BUILD_OUTPUT_DIRECTORY"'
```

打包后可用 `ldd simple_live_app/build/linux/x64/release/bundle/lib/libmedia_kit_video_plugin.so`
检查，除宿主系统自带的库（glibc、GTK、X11、GL、PipeWire 等）之外不应有
`not found`。
