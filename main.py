from kivy.app import App
from kivy.uix.label import Label

class TradingBotApp(App):
    def build(self):
        return Label(
            text="TradingBot Pro\n\nBase Build OK.\nNetwork code removed for testing.",
            halign="center"
        )

if __name__ == "__main__":
    TradingBotApp().run()