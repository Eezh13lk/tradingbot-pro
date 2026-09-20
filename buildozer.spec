[app]
title = TradingBot Pro
package.name = tradingbotpro
package.domain = org.tradingbotpro
source.dir = .
source.include_exts = py,kv,json,txt
source.exclude_dirs = bin,.buildozer,.venv,__pycache__
version = 1.0.0
requirements = python3,kivy==2.3.0,requests==2.31.0
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.debug = True
android.accept_sdk_license = True
android.ndk = 25b

[buildozer]
log_level = 2
warn_on_root = 1
