#!/bin/bash
# Move every screenshot across home into ~/Pictures/Screenshots and stop new ones hitting the Desktop.
mkdir -p ~/Pictures/Screenshots
n=$(find ~/Desktop ~/Downloads ~/Documents -maxdepth 2 -iname 'screenshot*' 2>/dev/null | wc -l | tr -d ' ')
find ~/Desktop ~/Downloads ~/Documents -maxdepth 2 -iname 'screenshot*' -exec mv -n {} ~/Pictures/Screenshots/ \; 2>/dev/null
echo "moved ~$n screenshots -> ~/Pictures/Screenshots"
defaults write com.apple.screencapture location ~/Pictures/Screenshots && killall SystemUIServer 2>/dev/null
echo "new screenshots now save to ~/Pictures/Screenshots (not Desktop)"
