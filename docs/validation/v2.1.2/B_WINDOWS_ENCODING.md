# B 批 Windows 编码回归

修复前：main@7d0e07962499851f71ca0a39d5b0132b5edcd5f5，[Run 38765334673](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38765334673) 的三个 Windows 作业均在 `test_current_readme_and_generated_analysis_package_use_release_version` 失败。README 使用默认 cp1252 解码，抛出 `UnicodeDecodeError: byte 0x8d in position 92`。两个 Windows backend 均 807 collected / 806 passed / 1 failed / 0 errors / 0 skipped。Ubuntu 与前端通过不能替代 Windows。

修复：本批新增版本同步脚本、版本回归与媒体清单读取均显式指定 UTF-8；不改变系统编码，不跳过测试、不放宽媒体守卫。后续同 SHA CI 作为跨平台转绿证据。

UTF-8 修正后本地严格全量：828 collected / 784 passed / 44 Windows 平台 skip / 0 failed / 0 errors；前端四项通过、44 passed。跨平台状态待本修正提交的 CI，不以本地结果代替。
