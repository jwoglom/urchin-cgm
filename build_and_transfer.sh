pebble build && adb push build/urchin-cgm.pbw /sdcard/ && adb shell am start -a "android.intent.action.VIEW" -t "application/x-pbw" -d "file:///sdcard/urchin-cgm.pbw"
