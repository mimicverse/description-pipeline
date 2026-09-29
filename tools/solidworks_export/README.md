# SolidWorks 历史兼容工具

旧原生导出、部署和独立打包入口已停用。生产入口见[Windows worker 说明](../../docs/sources/solidworks.md)。

本目录保留旧包读取、配置校验和 synthetic 回归；原生 COM 层转发到公共工具包。
回归中的变换与惯量计算用于独立核对，不作为第二套生产生成器。

```sh
python -m unittest discover -s tests/solidworks_export -t .
```

[迁移说明](MIGRATION.md)
