__copyright__ = 'Copyright 2023, 3Liz'
__license__ = 'GPL version 3'
__email__ = 'info@3liz.org'

from os.path import relpath
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from qgis.core import (
    QgsDataSourceUri,
    QgsLayerTree,
    QgsLayerTreeNode,
    QgsMapLayer,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsWkbTypes,
)

from lizmap.definitions.lizmap_cloud import CLOUD_DOMAIN
from lizmap.qgis_plugin_tools.tools.i18n import tr
from lizmap.tools import cast_to_group, cast_to_layer, is_vector_pg, update_uri
from lizmap.widgets.check_project import (
    RASTER_COUNT_CELL,
    Checks,
    Error,
    SourceGroup,
    SourceLayer,
)

""" Some checks which can be done on a layer. """

# https://github.com/3liz/lizmap-web-client/issues/3692

FORCE_LOCAL_FOLDER = tr('Prevent file based layers from being in a parent folder')
ALLOW_PARENT_FOLDER = tr('Allow file based layers to be in a parent folder')
PREVENT_OTHER_DRIVE = tr('Prevent file based layers from being stored on another drive (network or local)')
PREVENT_SERVICE = tr('Prevent PostgreSQL layers from using a service file')
PREVENT_AUTH_DB = tr('Prevent PostgreSQL layers from using the QGIS authentication database')
FORCE_PG_USER_PASS = tr(
    'PostgreSQL layers, if using a user and password, must have credentials saved in the datasource')
PREVENT_ECW = tr('Prevent from using a ECW raster')


def project_safeguards_checks(
        project: QgsProject,
        prevent_ecw: bool,
        prevent_auth_id: bool,
        prevent_service: bool,
        force_pg_user_pass: bool,
        prevent_other_drive: bool,
        allow_parent_folder: bool,
        parent_folder: str,
        lizmap_cloud: bool,
) -> Dict:
    """ Check the project about safeguards. """
    # Do not use homePath, it's not designed for this if the user has set a custom home path
    project_home = Path(project.absolutePath())
    results = {}
    checks = Checks()

    for layer in project.mapLayers().values():

        if isinstance(layer, QgsRasterLayer):
            if layer.source().lower().endswith('ecw') and prevent_ecw:
                results[SourceLayer(layer.name(), layer.id())] = checks.PreventEcw

        if is_vector_pg(layer):
            # Make a copy by using a string, so we are sure to have user or password
            datasource = QgsDataSourceUri(layer.source())
            if datasource.authConfigId() != '' and prevent_auth_id:
                results[SourceLayer(layer.name(), layer.id())] = checks.AuthenticationDb

                # We can continue
                continue

            if datasource.service() != '' and prevent_service:
                results[SourceLayer(layer.name(), layer.id())] = checks.PgService

                # We can continue
                continue

            if not datasource.service():
                if datasource.host().endswith(CLOUD_DOMAIN) or force_pg_user_pass:
                    if not datasource.username() or not datasource.password():
                        results[SourceLayer(layer.name(), layer.id())] = checks.PgForceUserPass

                    # We can continue
                    continue

        # Only vector/raster file based

        components = QgsProviderRegistry.instance().decodeUri(layer.dataProvider().name(), layer.source())
        if 'path' not in components.keys():
            # The layer is not file base.
            continue

        layer_path = Path(components['path'])
        try:
            if not layer_path.exists():
                # Let's skip, QGIS is already warning this layer
                # Or the file might be a COG on Linux :
                # /vsicurl/https://demo.snap.lizmap.com/lizmap_3_6/cog/...
                continue
        except OSError:
            # Ticket https://github.com/3liz/lizmap-plugin/issues/541
            # OSError: [WinError 123] La syntaxe du nom de fichier, de répertoire ou de volume est incorrecte:
            # '\\vsicurl\\https:\\XXX.lizmap.com\\YYY\\cog\\ZZZ.tif'
            continue

        try:
            relative_path = relpath(layer_path, project_home)
        except ValueError:
            # https://docs.python.org/3/library/os.path.html#os.path.relpath
            # On Windows, ValueError is raised when path and start are on different drives.
            # For instance, H: and C:

            if lizmap_cloud or prevent_other_drive:
                results[SourceLayer(layer.name(), layer.id())] = checks.PreventDrive
                continue

            # Not sure what to do for now...
            # We can't compute a relative path, but the user didn't enable the safety check, so we must still skip
            continue

        if allow_parent_folder:
            # The user allow parent folder, so we check against the string provided in the function call
            if parent_folder in relative_path:
                results[SourceLayer(layer.name(), layer.id())] = checks.PreventParentFolder
        else:
            # The user wants only local files, we only check for ".."
            if '..' in relative_path:
                results[SourceLayer(layer.name(), layer.id())] = checks.PreventParentFolder

        if isinstance(layer, QgsRasterLayer):
            # Only file based raster
            if not layer.dataProvider().hasPyramids():
                if layer.width() * layer.height() >= RASTER_COUNT_CELL:
                    results[SourceLayer(layer.name(), layer.id())] = checks.RasterWithoutPyramid

    return results


def project_invalid_pk(project: QgsProject) -> Tuple[List[SourceLayer], List[SourceLayer]]:
    """ Check either non existing PK or bigint. """
    autogenerated_keys = []
    int8 = []
    for layer in project.mapLayers().values():

        if not isinstance(layer, QgsVectorLayer):
            continue

        result, field = auto_generated_primary_key_field(layer)
        if result:
            autogenerated_keys.append(SourceLayer(layer.name(), layer.id()))

        if invalid_int8_primary_key(layer):
            int8.append(SourceLayer(layer.name(), layer.id()))

    return autogenerated_keys, int8


def auto_generated_primary_key_field(layer: QgsVectorLayer) -> Tuple[bool, Optional[str]]:
    """ If the primary key has been detected as tid/ctid but the field does not exist. """
    # Example
    # CREATE TABLE IF NOT EXISTS public.test_tid
    # (
    #     id bigint,
    #     label text
    # )
    # In QGIS source code, look for "Primary key is ctid"

    if layer.dataProvider().uri().keyColumn() == '':
        # GeoJSON etc
        return False, None

    # QgsVectorLayer.primaryKeyAttributes is returning a list.
    if len(layer.primaryKeyAttributes()) >= 2:
        # We don't check for composite keys here
        return False, None

    if layer.dataProvider().uri().keyColumn() in layer.fields().names():
        return False, None

    # The layer has "tid" or "ctid" as a primary key, but the field is not found.
    return True, layer.dataProvider().uri().keyColumn()


def invalid_int8_primary_key(layer: QgsVectorLayer) -> bool:
    """ If the layer has a primary key as int8, alias bigint. """
    # Example
    # CREATE TABLE IF NOT EXISTS france.test_bigint
    # (
    #     id bigint PRIMARY KEY,
    #     label text
    # )
    # QgsVectorLayer.primaryKeyAttributes is returning a list.
    if len(layer.primaryKeyAttributes()) != 1:
        # We might have either no primary key,
        # or a composite primary key
        return False

    uri = QgsDataSourceUri(layer.source())
    primary_key = uri.keyColumn()
    if not primary_key:
        return False

    if primary_key not in layer.fields().names():
        # The primary key used in the datasource doesn't exist in the proper layer fields
        # We don't check, because this test is done in "auto_generated_primary_key_field"
        return False

    field = layer.fields().field(primary_key)
    return field.typeName().lower() == 'int8'


def duplicated_layer_name_or_group(project: QgsProject) -> dict:
    """ The CFG can only store layer/group names which are unique. """
    result = {}
    # Vector and raster layers
    for layer in project.mapLayers().values():
        layer: QgsMapLayer
        name = layer.name()
        if name not in result.keys():
            result[name] = 1
        else:
            result[name] += 1

    # Groups
    for child in project.layerTreeRoot().children():
        if QgsLayerTree.isGroup(child):
            child = cast_to_group(child)
            name = child.name()
            if name not in result.keys():
                result[name] = 1
            else:
                result[name] += 1

    return result


def duplicated_layer_with_filter(project: QgsProject) -> Optional[str]:
    """ Check for duplicated layers with the same datasource but different filters. """
    unique_datasource = {}
    for layer in project.mapLayers().values():
        uri = QgsDataSourceUri(layer.source())
        uri_filter = uri.sql()
        if uri_filter == '':
            continue

        uri.setSql('')

        uri_string = uri.uri(True)

        if uri_string not in unique_datasource.keys():
            # First time we meet this datasource, we append
            unique_datasource[uri_string] = {}

        if uri_filter not in unique_datasource[uri_string]:
            # We add the filter with the layer name
            unique_datasource[uri_string][uri_filter] = layer.name()

    if len(unique_datasource.keys()) == 0:
        return None

    text = ''
    for datasource, filters in unique_datasource.items():
        if len(filters.values()) <= 1:
            continue

        layer_names = ','.join([f"'{k}'" for k in filters.values()])
        uri_filter = ','.join([f"'{k}'" for k in filters.keys()])
        text += tr(
            "Review layers <strong>{layers}</strong> having the same datasource '{datasource}' with these "
            "filters {uri_filter}."
        ).format(
            layers=layer_names,
            datasource=QgsDataSourceUri.removePassword(QgsDataSourceUri(datasource).uri(False)),
            uri_filter=uri_filter
        )
        text += '<br>'

    text += '<br>'
    text += tr(
        'Checkbox are supported natively in the legend. Using filters for the same '
        'datasource are highly discouraged.'
    )
    text += '<br>'
    return text


def simplify_provider_side(project: QgsProject, fix=False) -> List[SourceLayer]:
    """ Return the list of layer name which can be simplified on the server side. """
    results = []
    for layer in project.mapLayers().values():
        if not is_vector_pg(layer, geometry_check=True):
            continue

        if layer.geometryType() == QgsWkbTypes.PointGeometry:
            continue

        if not layer.simplifyMethod().forceLocalOptimization():
            continue

        results.append(SourceLayer(layer.name(), layer.id()))

        if fix:
            simplify = layer.simplifyMethod()
            simplify.setForceLocalOptimization(False)
            layer.setSimplifyMethod(simplify)

    return results


def use_estimated_metadata(project: QgsProject, fix: bool = False) -> List[SourceLayer]:
    """ Return the list of layer name which can use estimated metadata. """
    results = []
    for layer in project.mapLayers().values():
        if not is_vector_pg(layer, geometry_check=True):
            continue

        uri = layer.dataProvider().uri()
        if not uri.useEstimatedMetadata():
            results.append(SourceLayer(layer.name(), layer.id()))

            if fix:
                uri.setUseEstimatedMetadata(True)
                update_uri(layer, uri)

    return results


def project_trust_layer_metadata(project: QgsProject, fix: bool = False) -> bool:
    """ Trust layer metadata at the project level. """
    if not fix:
        return project.trustLayerMetadata()

    project.setTrustLayerMetadata(True)
    return True


def count_legend_items(layer_tree: QgsLayerTreeNode, project, list_qgs: list) -> list:
    """ Count all items in the project legend. """
    for child in layer_tree.children():
        # noinspection PyArgumentList
        if QgsLayerTree.isLayer(child):
            list_qgs.append(child.name())
        else:
            child = cast_to_group(child)
            list_qgs.append(child.name())
            # Recursive call
            list_qgs = count_legend_items(child, project, list_qgs)

    return list_qgs


def trailing_layer_group_name(layer_tree: QgsLayerTreeNode, project, results: List) -> List:
    """ Check for a trailing space in layer or group name. """
    for child in layer_tree.children():
        # noinspection PyArgumentList
        if QgsLayerTree.isLayer(child):
            child = cast_to_layer(child)
            layer = project.mapLayer(child.layerId())
            if layer.name().strip() != layer.name():
                results.append(
                    Error(layer.name(), Checks().LeadingTrailingSpaceLayerGroupName, SourceLayer(layer.name(), layer.id())))
        else:
            child = cast_to_group(child)
            if child.name().strip() != child.name():
                results.append(
                    Error(child.name(), Checks().LeadingTrailingSpaceLayerGroupName, SourceGroup))

            # Recursive call
            results = trailing_layer_group_name(child, project, results)

    return results
