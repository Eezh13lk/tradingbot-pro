from kivy.app import App
from kivy.uix.label import Label

class TradingBotApp(App):
    def build(self):
        return Label(
            text="TradingBot Pro\n\nBuild OK.\nNext: add bot/ code\nfrom the README.",
            halign="center"
        )

if __name__ == "__main__":
    TradingBotApp().run()
