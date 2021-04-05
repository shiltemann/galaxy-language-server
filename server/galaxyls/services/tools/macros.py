from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel
from pygls.lsp.types import Location
from pygls.workspace import Workspace
from galaxyls.services.tools.constants import IMPORT, NAME, TOKEN

from galaxyls.services.xml.document import XmlDocument
from galaxyls.services.xml.parser import XmlDocumentParser


class BaseMacrosModel(BaseModel):
    class Config:
        arbitrary_types_allowed = True


class TokenDefinition(BaseModel):
    name: str
    location: Location


class ImportedMacrosFile(BaseMacrosModel):
    file_name: str
    file_uri: Optional[str]
    document: Optional[XmlDocument]
    tokens: Dict[str, TokenDefinition]


class ToolMacroDefinitions(BaseMacrosModel):
    tool_document: XmlDocument
    imported_macros: Dict[str, ImportedMacrosFile]
    tokens: Dict[str, TokenDefinition]

    def go_to_import_definition(self, file_name: str) -> Optional[List[Location]]:
        imported_macros = self.imported_macros.get(file_name)
        if imported_macros and imported_macros.document and imported_macros.document.root:
            macros_file_uri = imported_macros.file_uri
            content_range = imported_macros.document.get_element_name_range(imported_macros.document.root)
            if content_range:
                return [
                    Location(
                        uri=macros_file_uri,
                        range=content_range,
                    )
                ]

    def get_token_definition(self, token: str) -> Optional[TokenDefinition]:
        return self.tokens.get(token)


class MacroDefinitionsProvider:
    """Provides location information about macros imported by a tool."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def load_macro_definitions(self, tool_xml: XmlDocument) -> ToolMacroDefinitions:
        tokens = self._get_token_definitions(tool_xml)
        imported_macro_files = self._get_imported_macro_files_from_tool(tool_xml)
        for file in imported_macro_files.values():
            tokens.update(file.tokens)
        return ToolMacroDefinitions(tool_document=tool_xml, imported_macros=imported_macro_files, tokens=tokens)

    def _get_imported_macro_files_from_tool(self, tool_xml: XmlDocument) -> Dict[str, ImportedMacrosFile]:
        tool_directory = self._get_tool_directory(tool_xml)
        macro_files = {}
        import_elements = tool_xml.find_all_elements_with_name(IMPORT)
        for imp in import_elements:
            name = imp.get_content(tool_xml.document.source)
            if name:
                path = tool_directory / name
                file_uri = None
                macros_document = None
                tokens = []
                if path.exists():
                    file_uri = path.as_uri()
                    macros_document = self._load_macros_document(file_uri)
                    tokens = self._get_token_definitions(macros_document)
                macro_files[name] = ImportedMacrosFile(
                    file_name=name, file_uri=file_uri, document=macros_document, tokens=tokens
                )
        return macro_files

    def _get_tool_directory(self, tool_xml: XmlDocument):
        tool_directory = Path(tool_xml.document.path).resolve().parent
        return tool_directory

    def _load_macros_document(self, document_uri: str) -> XmlDocument:
        document = self.workspace.get_document(document_uri)
        xml_document = XmlDocumentParser().parse(document)
        return xml_document

    def _get_token_definitions(self, macros_xml: XmlDocument) -> Dict[str, TokenDefinition]:
        token_elements = macros_xml.find_all_elements_with_name(TOKEN)
        rval = {}
        for element in token_elements:
            token_def = TokenDefinition(
                name=element.get_attribute(NAME).replace("@", ""),
                location=Location(
                    uri=macros_xml.document.uri,
                    range=macros_xml.get_element_name_range(element),
                ),
            )
            rval[token_def.name] = token_def
        return rval
