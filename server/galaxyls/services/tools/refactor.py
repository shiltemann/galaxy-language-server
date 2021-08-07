from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from lxml import etree
from pydantic.main import BaseModel
from pygls.lsp.types import (
    CodeAction,
    CodeActionKind,
    CodeActionParams,
    CreateFile,
    Position,
    Range,
    ResourceOperationKind,
    TextDocumentEdit,
    TextEdit,
    VersionedTextDocumentIdentifier,
    WorkspaceEdit,
)
from pygls.workspace import Workspace

from galaxyls.services.format import GalaxyToolFormatService
from galaxyls.services.tools.constants import DESCRIPTION, MACRO, MACROS, TOOL, XML, XREF
from galaxyls.services.tools.document import GalaxyToolXmlDocument
from galaxyls.services.tools.macros import ImportedMacrosFile, MacroDefinitionsProvider, ToolMacroDefinitions
from galaxyls.services.xml.document import XmlDocument

DEFAULT_MACROS_FILENAME = "macros.xml"


class MacroData(BaseModel):
    name: str
    content: str


class RefactorMacrosService:
    def __init__(
        self,
        workspace: Workspace,
        macro_definitions_provider: MacroDefinitionsProvider,
        format_service: GalaxyToolFormatService,
    ) -> None:
        self.workspace = workspace
        self.definitions_provider = macro_definitions_provider
        self.format_service = format_service

    def create_extract_to_local_macro_actions(
        self, tool: GalaxyToolXmlDocument, macro: MacroData, params: CodeActionParams
    ) -> List[CodeAction]:
        return [
            CodeAction(
                title="Extract to local macro",
                kind=CodeActionKind.RefactorExtract,
                edit=WorkspaceEdit(changes=self._calculate_local_changes_for_macro(tool, macro, params)),
            )
        ]

    def create_extract_to_macros_file_actions(
        self, tool: GalaxyToolXmlDocument, macro_definitions: ToolMacroDefinitions, macro: MacroData, params: CodeActionParams
    ) -> List[CodeAction]:
        if not macro_definitions.imported_macros:
            return [
                CodeAction(
                    title=f"Extract to macro, create and import {DEFAULT_MACROS_FILENAME}",
                    kind=CodeActionKind.RefactorExtract,
                    edit=WorkspaceEdit(
                        document_changes=self._calculate_external_changes_for_macro_in_new_file(
                            tool, DEFAULT_MACROS_FILENAME, macro, params
                        )
                    ),
                )
            ]
        code_actions = []
        for file_name, macro_file_definition in macro_definitions.imported_macros.items():
            code_actions.append(
                CodeAction(
                    title=f"Extract to macro in {file_name}",
                    kind=CodeActionKind.RefactorExtract,
                    edit=WorkspaceEdit(
                        changes=self._calculate_external_changes_for_macro(tool, macro_file_definition, macro, params)
                    ),
                )
            )

        return code_actions

    def _calculate_local_changes_for_macro(
        self, tool: GalaxyToolXmlDocument, macro: MacroData, params: CodeActionParams
    ) -> Dict[str, TextEdit]:
        macros_element = tool.get_macros_element()
        edits: List[TextEdit] = []
        if macros_element is None:
            edits.append(self._edit_create_with_macros_section(tool, macro))
        else:
            edits.append(self._edit_add_macro_to_macros_section(tool, macro))
        edits.append(self._edit_replace_range_with_macro_expand(tool, macro, params.range))
        changes = {params.text_document.uri: edits}
        return changes

    def _calculate_external_changes_for_macro(
        self,
        tool: GalaxyToolXmlDocument,
        macro_file_definition: ImportedMacrosFile,
        macro: MacroData,
        params: CodeActionParams,
    ) -> Dict[str, TextEdit]:
        macros_xml_doc = macro_file_definition.document
        macros_root = macros_xml_doc.root
        insert_position = macros_xml_doc.get_position_after_last_child(macros_root)
        insert_range = Range(start=insert_position, end=insert_position)
        macro_xml = f'<xml name="{macro.name}">\n{macro.content}\n</xml>'
        final_macro_xml = self._adapt_format(macros_xml_doc, insert_range, macro_xml)
        external_edit = TextEdit(
            range=insert_range,
            new_text=final_macro_xml,
        )
        changes = {
            params.text_document.uri: [self._edit_replace_range_with_macro_expand(tool, macro, params.range)],
            macro_file_definition.file_uri: [external_edit],
        }
        return changes

    def _calculate_external_changes_for_macro_in_new_file(
        self, tool: GalaxyToolXmlDocument, new_file_name: str, macro: MacroData, params: CodeActionParams
    ):
        base_path = Path(urlparse(tool.xml_document.document.uri).path).parent
        new_file_uri = (base_path / new_file_name).as_uri()
        xml_content = f'<macros>\n<xml name="{macro.name}">\n{macro.content}\n</xml>\n</macros>'
        final_xml_content = self.format_service.format_content(xml_content)
        new_doc_insert_position = Position(line=0, character=0)
        tool_document = self.workspace.get_document(params.text_document.uri)
        changes = [
            CreateFile(uri=new_file_uri, kind=ResourceOperationKind.Create),
            TextDocumentEdit(
                text_document=VersionedTextDocumentIdentifier(
                    uri=new_file_uri,
                    version=0,
                ),
                edits=[
                    TextEdit(
                        range=Range(start=new_doc_insert_position, end=new_doc_insert_position),
                        new_text=final_xml_content,
                    ),
                ],
            ),
            TextDocumentEdit(
                text_document=VersionedTextDocumentIdentifier(
                    uri=tool_document.uri,
                    version=tool_document.version,
                ),
                edits=[
                    self._edit_create_import_macros_section(tool, DEFAULT_MACROS_FILENAME),
                    self._edit_replace_range_with_macro_expand(tool, macro, params.range),
                ],
            ),
        ]
        return changes

    def _edit_replace_range_with_macro_expand(self, tool: GalaxyToolXmlDocument, macro: MacroData, range: Range) -> TextEdit:
        indentation = tool.xml_document.get_line_indentation(range.start.line)
        return TextEdit(
            range=self._get_range_from_line_start(range),
            new_text=f'{indentation}<expand macro="{macro.name}"/>',
        )

    def _edit_create_import_macros_section(self, tool: GalaxyToolXmlDocument, macros_file_name: str) -> TextEdit:
        macros_element = tool.find_element(MACROS)
        if macros_element:
            insert_position = tool.get_position_before_first_child(macros_element)
            macro_xml = f"<import>{macros_file_name}</import>"
        else:
            insert_position = self._find_macros_insert_position(tool)
            macro_xml = f"<macros>\n<import>{macros_file_name}</import>\n</macros>"
        insert_range = Range(start=insert_position, end=insert_position)
        final_macro_xml = self._adapt_format(tool.xml_document, insert_range, macro_xml)
        return TextEdit(
            range=insert_range,
            new_text=final_macro_xml,
        )

    def _edit_create_with_macros_section(self, tool: GalaxyToolXmlDocument, macro: MacroData) -> TextEdit:
        insert_position = self._find_macros_insert_position(tool)
        insert_range = Range(start=insert_position, end=insert_position)
        macro_xml = f'<macros>\n<xml name="{macro.name}">\n{macro.content}\n</xml>\n</macros>'
        final_macro_xml = self._adapt_format(tool.xml_document, insert_range, macro_xml)
        return TextEdit(
            range=insert_range,
            new_text=final_macro_xml,
        )

    def _edit_add_macro_to_macros_section(self, tool: GalaxyToolXmlDocument, macro: MacroData) -> TextEdit:
        macros_element = tool.get_macros_element()
        insert_position = tool.get_position_after_last_child(macros_element)
        insert_range = Range(start=insert_position, end=insert_position)
        macro_xml = f'<xml name="{macro.name}">\n{macro.content}\n</xml>'
        final_macro_xml = self._adapt_format(tool.xml_document, insert_range, macro_xml)
        return TextEdit(
            range=insert_range,
            new_text=final_macro_xml,
        )

    def _find_macros_insert_position(self, tool: GalaxyToolXmlDocument) -> Position:
        """Returns the position inside the document where the macros section
        can be inserted.

        Returns:
            Range: The position where the macros section can be inserted in the document.
        """
        section = tool.find_element(XREF)
        if section:
            return tool.get_position_after(section)
        section = tool.find_element(DESCRIPTION)
        if section:
            return tool.get_position_after(section)
        return tool.get_content_range(TOOL).start

    def _get_range_from_line_start(self, range: Range) -> Range:
        return Range(start=Position(line=range.start.line, character=0), end=range.end)

    def _apply_indent(self, text: str, indent: str) -> str:
        indented = indent + text.replace("\n", "\n" + indent)
        return indented

    def _adapt_format(
        self, xml_document: XmlDocument, insert_range: Range, xml_text: str, insert_in_new_line: bool = True
    ) -> str:
        formatted_macro = self.format_service.format_content(xml_text).rstrip()
        reference_line = insert_range.start.line
        if not insert_in_new_line:
            reference_line -= 1
        indent = xml_document.get_line_indentation(reference_line)
        final_macro_text = self._apply_indent(formatted_macro, indent)
        if insert_in_new_line:
            return f"\n{final_macro_text}"
        return final_macro_text


class RefactoringService:
    def __init__(self, macros_refactoring_service: RefactorMacrosService) -> None:
        self.macros = macros_refactoring_service

    def get_available_refactoring_actions(self, xml_document: XmlDocument, params: CodeActionParams) -> List[CodeAction]:
        code_actions = []
        text_in_range = xml_document.get_text_in_range(params.range)
        target_element_tag = self.get_valid_full_element_tag(text_in_range)
        if target_element_tag is not None:
            macro = MacroData(name=target_element_tag, content=text_in_range.strip())
            macro_definitions = self.macros.definitions_provider.load_macro_definitions(xml_document)
            tool = GalaxyToolXmlDocument.from_xml_document(xml_document)
            code_actions.extend(self.macros.create_extract_to_macros_file_actions(tool, macro_definitions, macro, params))
            code_actions.extend(self.macros.create_extract_to_local_macro_actions(tool, macro, params))
        return code_actions

    def get_valid_full_element_tag(self, xml_text: str) -> Optional[str]:
        stripped_xml = xml_text.strip()
        if len(stripped_xml) < 5 or (stripped_xml[0] != "<" or stripped_xml[-1] != ">"):
            # Too short to be an element or doesn't look like an element
            return None
        return self._get_valid_node_tag(stripped_xml)

    def _get_valid_node_tag(self, stripped_xml: str) -> Optional[str]:
        try:
            xml_in_range = etree.fromstring(stripped_xml, etree.XMLParser(strip_cdata=False))
            if xml_in_range.tag in [TOOL, MACROS, MACRO, XML]:
                return None
            return xml_in_range.tag
        except BaseException as e:
            print(e)
            return None
