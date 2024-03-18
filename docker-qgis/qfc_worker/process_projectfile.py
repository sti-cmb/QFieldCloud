import logging
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree

from qgis.core import (
    QgsLayerTree,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsProject,
)
from qgis.PyQt.QtCore import QEventLoop, QSize
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtXml import QDomDocument

from .utils import (
    FailedThumbnailGenerationException,
    InvalidFileExtensionException,
    InvalidXmlFileException,
    ProjectFileNotFoundException,
    get_layers_data,
    get_qgis_xml_error_context,
    layers_data_to_string,
)

logger = logging.getLogger("PROCPRJ")


def check_valid_project_file(project_filename: Path) -> None:
    logger.info("Check QGIS project file validity…")

    if not project_filename.exists():
        raise ProjectFileNotFoundException(project_filename=project_filename)

    if project_filename.suffix == ".qgs":
        with open(project_filename, "rb") as fh:
            try:
                for event, elem in ElementTree.iterparse(fh):
                    continue
            except ElementTree.ParseError as error:
                error_msg = str(error)
                raise InvalidXmlFileException(
                    xml_error=get_qgis_xml_error_context(error_msg, fh) or error_msg,
                    project_filename=project_filename,
                )
    elif project_filename.suffix != ".qgz":
        raise InvalidFileExtensionException(
            project_filename=project_filename, extension=project_filename.suffix
        )

    logger.info("QGIS project file is valid!")


def extract_project_details(project: QgsProject) -> dict[str, str]:
    """Extract project details"""
    logger.info("Extract project details…")

    details = {}

    logger.info("Reading QGIS project file…")
    map_settings = QgsMapSettings()

    def on_project_read_wrapper(
        tmp_project: QgsProject,
    ) -> Callable[[QDomDocument], None]:
        def on_project_read(doc: QDomDocument) -> None:
            r, _success = tmp_project.readNumEntry("Gui", "/CanvasColorRedPart", 255)
            g, _success = tmp_project.readNumEntry("Gui", "/CanvasColorGreenPart", 255)
            b, _success = tmp_project.readNumEntry("Gui", "/CanvasColorBluePart", 255)
            background_color = QColor(r, g, b)
            map_settings.setBackgroundColor(background_color)

            details["background_color"] = background_color.name()

            nodes = doc.elementsByTagName("mapcanvas")

            for i in range(nodes.size()):
                node = nodes.item(i)
                element = node.toElement()
                if (
                    element.hasAttribute("name")
                    and element.attribute("name") == "theMapCanvas"
                ):
                    map_settings.readXml(node)

            map_settings.setRotation(0)
            map_settings.setOutputSize(QSize(1024, 768))

            details["extent"] = map_settings.extent().asWktPolygon()

        return on_project_read

    # NOTE use a temporary project to get the project extent and background color
    # as we can disable resolving layers, which results in great speed gains
    tmp_project = QgsProject()
    tmp_project_read_flags = (
        # TODO we use `QgsProject` read flags, as the ones in `Qgis.ProjectReadFlags` do not work in QGIS 3.34.2
        QgsProject.ReadFlags()
        | QgsProject.FlagDontResolveLayers
        | QgsProject.FlagDontLoadLayouts
        | QgsProject.FlagDontLoad3DViews
        | QgsProject.DontLoadProjectStyles
    )
    tmp_project.readProject.connect(on_project_read_wrapper(tmp_project))
    tmp_project.read(project.fileName(), tmp_project_read_flags)

    # NOTE force delete the `QgsProject`, otherwise the `QgsApplication` might be deleted by the time the project is garbage collected
    del tmp_project

    details["crs"] = project.crs().authid()
    details["project_name"] = project.title()

    logger.info("Extracting layer and datasource details…")

    details["layers_by_id"] = get_layers_data(project)
    details["ordered_layer_ids"] = list(details["layers_by_id"].keys())
    details["attachment_dirs"], _ = project.readListEntry(
        "QFieldSync", "attachmentDirs", ["DCIM"]
    )

    logger.info(
        f'QGIS project layer checks\n{layers_data_to_string(details["layers_by_id"])}',
    )

    return details


def generate_thumbnail(project_filename: str, thumbnail_filename: Path) -> None:
    """Create a thumbnail for the project

    As from https://docs.qgis.org/3.16/en/docs/pyqgis_developer_cookbook/composer.html#simple-rendering

    Args:
        project_filename (str)
        thumbnail_filename (Path)
    """
    logger.info("Generate project thumbnail image…")

    map_settings = QgsMapSettings()

    def on_project_read_wrapper(
        tmp_project: QgsProject,
        tmp_layer_tree: QgsLayerTree,
    ) -> Callable[[QDomDocument], None]:
        def on_project_read(doc: QDomDocument) -> None:
            r, _success = tmp_project.readNumEntry("Gui", "/CanvasColorRedPart", 255)
            g, _success = tmp_project.readNumEntry("Gui", "/CanvasColorGreenPart", 255)
            b, _success = tmp_project.readNumEntry("Gui", "/CanvasColorBluePart", 255)
            map_settings.setBackgroundColor(QColor(r, g, b))

            nodes = doc.elementsByTagName("mapcanvas")

            for i in range(nodes.size()):
                node = nodes.item(i)
                element = node.toElement()
                if (
                    element.hasAttribute("name")
                    and element.attribute("name") == "theMapCanvas"
                ):
                    map_settings.readXml(node)

            map_settings.setRotation(0)
            map_settings.setTransformContext(tmp_project.transformContext())
            map_settings.setPathResolver(tmp_project.pathResolver())
            map_settings.setOutputSize(QSize(100, 100))
            map_settings.setLayers(reversed(list(tmp_layer_tree.customLayerOrder())))

        return on_project_read

    # NOTE use a temporary project to generate the layer rendering with improved speed
    tmp_project = QgsProject()
    tmp_layer_tree = tmp_project.layerTreeRoot()
    tmp_project_read_flags = (
        # TODO we use `QgsProject` read flags, as the ones in `Qgis.ProjectReadFlags` do not work in QGIS 3.34.2
        QgsProject.ReadFlags()
        | QgsProject.ForceReadOnlyLayers
        | QgsProject.FlagDontLoadLayouts
        | QgsProject.FlagDontLoad3DViews
        | QgsProject.DontLoadProjectStyles
    )
    tmp_project.readProject.connect(
        on_project_read_wrapper(tmp_project, tmp_layer_tree)
    )
    tmp_project.read(
        project_filename,
        tmp_project_read_flags,
    )

    renderer = QgsMapRendererParallelJob(map_settings)

    event_loop = QEventLoop()
    renderer.finished.connect(event_loop.quit)
    renderer.start()

    event_loop.exec_()

    img = renderer.renderedImage()

    if not img.save(str(thumbnail_filename)):
        raise FailedThumbnailGenerationException(reason="Failed to save.")

    # NOTE force delete the `QgsProject`, otherwise the `QgsApplication` might be deleted by the time the project is garbage collected
    del tmp_project

    logger.info("Project thumbnail image generated!")


if __name__ == "__main__":
    from qfieldcloud.qgis.utils import setup_basic_logging_config

    setup_basic_logging_config()
