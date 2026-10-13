"""Language codes and English names (Whisper's 100 languages, plus a few translation-only ones)."""
from __future__ import annotations

WHISPER = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani",
    "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian", "bn": "Bengali", "bo": "Tibetan",
    "br": "Breton", "bs": "Bosnian", "ca": "Catalan", "cs": "Czech", "cy": "Welsh",
    "da": "Danish", "de": "German", "el": "Greek", "en": "English", "es": "Spanish",
    "et": "Estonian", "eu": "Basque", "fa": "Persian", "fi": "Finnish", "fo": "Faroese",
    "fr": "French", "gl": "Galician", "gu": "Gujarati", "ha": "Hausa", "haw": "Hawaiian",
    "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "ht": "Haitian Creole", "hu": "Hungarian",
    "hy": "Armenian", "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese",
    "jw": "Javanese", "ka": "Georgian", "kk": "Kazakh", "km": "Khmer", "kn": "Kannada",
    "ko": "Korean", "la": "Latin", "lb": "Luxembourgish", "ln": "Lingala", "lo": "Lao",
    "lt": "Lithuanian", "lv": "Latvian", "mg": "Malagasy", "mi": "Maori", "mk": "Macedonian",
    "ml": "Malayalam", "mn": "Mongolian", "mr": "Marathi", "ms": "Malay", "mt": "Maltese",
    "my": "Burmese", "ne": "Nepali", "nl": "Dutch", "nn": "Norwegian Nynorsk", "no": "Norwegian",
    "oc": "Occitan", "pa": "Punjabi", "pl": "Polish", "ps": "Pashto", "pt": "Portuguese",
    "ro": "Romanian", "ru": "Russian", "sa": "Sanskrit", "sd": "Sindhi", "si": "Sinhala",
    "sk": "Slovak", "sl": "Slovenian", "sn": "Shona", "so": "Somali", "sq": "Albanian",
    "sr": "Serbian", "su": "Sundanese", "sv": "Swedish", "sw": "Swahili", "ta": "Tamil",
    "te": "Telugu", "tg": "Tajik", "th": "Thai", "tk": "Turkmen", "tl": "Tagalog",
    "tr": "Turkish", "tt": "Tatar", "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek",
    "vi": "Vietnamese", "yi": "Yiddish", "yo": "Yoruba", "zh": "Chinese", "yue": "Cantonese",
}

# Subtitle targets: anything the LLM can write, which includes a few Whisper can't hear.
TARGETS = {**WHISPER, "ku": "Kurdish"}

# Shown first in the pickers.
COMMON = ["ar", "en", "da", "he", "fr", "de", "sv", "no", "es"]


def name(code: str) -> str:
    return TARGETS.get(code, code)
