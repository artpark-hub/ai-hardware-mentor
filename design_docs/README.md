# Arduino Vision Interface — Quick Start

## 1. Install dependencies
Open Command Prompt in this folder and run:
```
pip install pyserial opencv-python anthropic Pillow
```

## 2. Run the app
```
python app.py
```

## 3. First-time setup inside the app

| Step | What to do |
|------|-----------|
| API Key | Paste your Anthropic API key in the top bar |
| Serial | Select COM4, click **▶ Connect Serial** |
| Camera | Click **▶ Start Camera** (try index 1 for DroidCam; use 0 if it doesn't work) |

## 4. Using the AI Assistant
- Type what you want to change in the prompt box (bottom right)
- Press **Ctrl+Enter** or click **✨ Ask AI**
- The AI sees your current sketch + serial log + camera snapshot
- If it generates new code, click **📋 Apply Last Code** to put it in the editor
- Click **⬆ Upload to Arduino** to flash it

## 5. Troubleshooting

**Camera index:** DroidCam via USB = usually index 1. If blank, try 0 or 2.

**Upload fails:** Make sure `arduino-cli` is installed and accessible:
```
arduino-cli version
arduino-cli core install arduino:avr
```

**Serial error:** Make sure Arduino IDE's Serial Monitor is closed before connecting here.
