# Copyright 2026 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from PyQt5.QtCore import Qt, QLocale
from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout, QLabel, QComboBox

from hscommon.trans import trget
from qt.preferences import get_langnames

tr = trget("ui")


class LanguageDialog(QDialog):
    """First-run dialog used to pick the interface language before the main UI is built."""

    def __init__(self, parent=None):
        flags = Qt.CustomizeWindowHint | Qt.WindowTitleHint | Qt.WindowSystemMenuHint
        super().__init__(parent, flags)
        self.setWindowTitle(tr("Select Language"))
        self._setupUi()
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)

    def _setupUi(self):
        self.layout = QVBoxLayout(self)
        self.label = QLabel(tr("Please select your language:"), self)
        self.layout.addWidget(self.label)
        self.languageComboBox = QComboBox(self)
        self.supportedLanguages = dict(sorted(get_langnames().items(), key=lambda item: item[1]))
        for lang_code, lang_str in self.supportedLanguages.items():
            self.languageComboBox.addItem(lang_str, userData=lang_code)
        self._setDefaultLanguage()
        self.layout.addWidget(self.languageComboBox)
        self.buttonBox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.layout.addWidget(self.buttonBox)

    def _setDefaultLanguage(self):
        # Prefer a full locale match (e.g. "zh_CN"), then fall back to the 2-letter code
        # (e.g. "en"), and finally to English.
        system_name = QLocale.system().name()
        lang = system_name if system_name in self.supportedLanguages else system_name[:2]
        if lang not in self.supportedLanguages:
            lang = "en"
        index = self.languageComboBox.findData(lang)
        if index >= 0:
            self.languageComboBox.setCurrentIndex(index)

    def selectedLanguage(self):
        return self.languageComboBox.currentData()
