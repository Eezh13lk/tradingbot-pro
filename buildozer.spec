[app]
title = TradingBot Pro
package.name = tradingbotpro
package.domain = org.tradingbotpro
source.dir = .
source.include_exts = py,kv,json,txt
source.exclude_dirs = bin,.buildozer,venv,.venv,__pycache__
version = 1.0.0
requirements = python3,kivy==2.3.1,requests==2.31.0,urllib3==1.26.18,certifi==2023.11.17,idna==3.6,chardet==5.2.0
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a
android.debug = True
p4a.branch = develop

[buildozer]
log_level = 2
warn_on_root = 1