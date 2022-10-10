from __future__ import annotations

import copy
import os
import shutil
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

import zarr
from anndata import AnnData
from ome_zarr.io import parse_url
from xarray import DataArray

from spatialdata._core.coordinate_system import CoordinateSystem, CoordSystem_t
from spatialdata._core.elements import Image, Labels, Points, Polygons
from spatialdata._core.transform import (
    Affine,
    BaseTransformation,
    get_transformation_from_dict,
)
from spatialdata._io.write import write_table

# def spatialdata_from_base_elements(
#     images: Optional[Dict[str, Image]] = None,
#     labels: Optional[Dict[str, Labels]] = None,
#     points: Optional[Dict[str, Points]] = None,
#     polygons: Optional[Dict[str, Polygons]] = None,
#     table: Optional[AnnData] = None,
# ) -> SpatialData:
#     # transforms
#     images_transforms = {k: t for k, t in images.items()} if images is not None else None
#     labels_transforms = {k: t for k, t in labels.items()} if labels is not None else None
#     points_transforms = {k: t for k, t in points.items()} if points is not None else None
#     polygons_transforms = {k: t for k, t in polygons.items()} if polygons is not None else None
#     # axes information
#     # TODO:
#
#     return SpatialData(
#         images=images if images is not None else {},
#         labels=labels if labels is not None else {},
#         points=points if points is not None else {},
#         polygons=polygons if polygons is not None else {},
#         table=table,
#         images_transforms=images_transforms,
#         labels_transforms=labels_transforms,
#         points_transforms=points_transforms,
#         polygons_transforms=polygons_transforms,
#     )


class SpatialData:
    """Spatial data structure."""

    images: Mapping[str, Image] = MappingProxyType({})
    labels: Mapping[str, Labels] = MappingProxyType({})
    points: Mapping[str, Points] = MappingProxyType({})
    polygons: Mapping[str, Polygons] = MappingProxyType({})
    _table: Optional[AnnData] = None

    def __init__(
        self,
        # base elements
        images: Mapping[str, Any] = MappingProxyType({}),
        labels: Mapping[str, Any] = MappingProxyType({}),
        points: Mapping[str, Any] = MappingProxyType({}),
        polygons: Mapping[str, Any] = MappingProxyType({}),
        table: Optional[AnnData] = None,
        # axes information
        images_axes: Optional[Mapping[str, Tuple[str, ...]]] = None,
        labels_axes: Optional[Mapping[str, Tuple[str, ...]]] = None,
        # transformations and coordinate systems
        transformations: Mapping[(str, str), Optional[Union[BaseTransformation, Dict[Any]]]] = MappingProxyType({}),
        coordinate_systems: Optional[List[Union[CoordSystem_t, CoordinateSystem]]] = None,
    ) -> None:
        if coordinate_systems is None:
            raise ValueError("Coordinate systems must be provided.")
        self.file_path: Optional[str] = None

        # reorders the axes to follow the ngff 0.4 convention (t, c, z, y, x)
        for d_x, d_axes in zip([images, labels], [images_axes, labels_axes]):
            for k in d_x.keys():
                x = d_x[k]
                axes = d_axes[k]
                new_x, new_axes = _validate_axes(x, axes)
                d_x[k] = new_x
                d_axes[k] = new_axes

        validated_coordinate_systems = _validate_coordinate_systems(coordinate_systems)
        for (src, des), transform in transformations.items():
            if transform is not None:
                continue
            else:
                ss = src.split("/")
                assert len(ss) == 3
                assert ss[0] == ""
                prefix, name = ss[1:]
                if prefix == "images":
                    src_axes = images_axes[name]
                elif prefix == "labels":
                    src_axes = labels_axes[name]
                else:
                    if prefix == "points":
                        ndim = points[name].obsm["spatial"].shape[1]
                    elif prefix == "polygons":
                        ndim = Polygons.string_to_tensor(polygons[name].obs["spatial"][0]).shape[1]
                    else:
                        raise ValueError(f"Element {element} not supported.")
                    src_axes = ("x", "y", "z")[:ndim]
                des_axes_obj = tuple(validated_coordinate_systems[des].axes)
                des_axes = tuple(axis.name for axis in des_axes_obj)
                affine_matrix = Affine._get_affine_iniection_from_axes(src_axes, des_axes)
                transformations[(src, des)] = {"type": "affine", "affine": affine_matrix[:-1, :].tolist()}

        for element_class, elements, prefix in zip(
            [Image, Labels, Points, Polygons],
            [images, labels, points, polygons],
            ["images", "labels", "points", "polygons"],
        ):
            self.__setattr__(prefix, {})
            validated_transformations = _validate_transformations(
                list(elements.keys()), prefix, transformations, validated_coordinate_systems
            )
            for name, data in elements.items():
                alignment_info = {
                    validated_coordinate_systems[des]: validated_transformations[f"/{prefix}/{name}"][des]
                    for des in validated_transformations[f"/{prefix}/{name}"]
                }
                if prefix == "images":
                    kw = {"axes": images_axes[name]}
                elif prefix == "labels":
                    kw = {"axes": labels_axes[name]}
                else:
                    kw = {}
                obj = element_class(data, alignment_info=alignment_info, **kw)
                self.__getattribute__(prefix)[name] = obj

        if table is not None:
            self._table = table

    def save_element(
        self, element_type: str, name: str, overwrite: bool = False, zarr_root: Optional[zarr.Group] = None
    ):
        if element_type not in ["images", "labels", "points", "polygons"]:
            raise ValueError(f"Element type {element_type} not supported.")
        if zarr_root is None:
            if not self.is_backed():
                raise ValueError("No backed storage found")
            store = parse_url(self.file_path, mode="a").store
            root = zarr.group(store=store)
        else:
            root = zarr_root
        if overwrite:
            if element_type in "images":
                raise ValueError(
                    "Overwriting images is not supported. This is a current limitation of the storage (labels may be "
                    "be contained in the same zarr group as the images). Please open a GitHue issue and we will "
                    "address this problem."
                )
            full_path_group = os.path.join(root.path, f"{name}/{element_type}/{name}")
            if os.path.isdir(full_path_group):
                shutil.rmtree(full_path_group)
        elem_group = root.require_group(name=name)
        self.__getattribute__(element_type)[name].to_zarr(elem_group, name=name)

    def write(self, file_path: str) -> None:
        """Write to Zarr file."""

        store = parse_url(file_path, mode="w").store
        root = zarr.group(store=store)

        # get union of unique ids of all elements
        elems = set().union(*[set(i) for i in [self.images, self.labels, self.points, self.polygons]])

        for el in elems:
            for element_type in ["images", "labels", "points", "polygons"]:
                if el in self.__getattribute__(element_type):
                    self.save_element(element_type, el, zarr_root=root)

        if self.table is not None:
            write_table(tables=self.table, group=root, name="table")

    @property
    def table(self) -> AnnData:
        return self._table

    @classmethod
    def read(
        cls, file_path: str, coordinate_system_names: Optional[Union[str, List[str]]] = None, filter_table: bool = False
    ) -> SpatialData:
        """

        Parameters
        ----------
        file_path : str
            The path to the zarr store or the zarr group.
        coordinate_system_names : Optional[Union[str, List[str]]]
            The names of the coordinate systems to read. If None, all coordinate systems are read.
        filter_table : bool
            If True, the table is filtered to only contain rows that are associated to regions in the specified
            coordinate systems.
        Returns
        -------
        SpatialData
            The spatial data object.

        """

        from spatialdata._io.read import read_zarr

        sdata = read_zarr(file_path, coordinate_system_names=coordinate_system_names, filter_table=filter_table)
        sdata.file_path = file_path
        return sdata

    def is_backed(self) -> bool:
        return self.file_path is not None

    def filter_by_coordinate_system(self, coordinate_system_names: Union[str, List[str]]) -> SpatialData:
        """Filter the spatial data by coordinate system names.

        Parameters
        ----------
        coordinate_system_names
            The coordinate system names to filter by.

        Returns
        -------
        SpatialData
            The filtered spatial data.
        """
        # easy to implement if everything is in memory, but requires more care when some information is in a backed
        # from a
        # file/cloud storage
        raise NotImplementedError("Filtering by coordinate system names is not yet implemented.")

    def _gen_spatial_elements(self):
        # notice that this does not return a table, so we assume that the table does not contain spatial information;
        # this needs to be checked in the future as the specification evolves
        for k in ["images", "labels", "points", "polygons"]:
            d = getattr(self, k)
            for name, obj in d.items():
                yield k, name, obj

    @property
    def coordinate_systems(self) -> List[CoordinateSystem]:
        ##
        all_cs = {}
        gen = self._gen_spatial_elements()
        for _, _, obj in gen:
            for name, cs in obj.coordinate_systems.items():
                if name in all_cs:
                    added = all_cs[name]
                    assert cs == added
                else:
                    all_cs[name] = cs
        ##
        return list(all_cs.values())

    def __repr__(self) -> str:
        return self._gen_repr()

    def _gen_repr(
        self,
    ) -> str:
        def rreplace(s: str, old: str, new: str, occurrence: int) -> str:
            """Reverse replace a up to a certain number of occurences."""
            li = s.rsplit(old, occurrence)
            return new.join(li)

        def h(s: str) -> str:
            return s
            # return hashlib.md5(repr(s).encode()).hexdigest()

        ##
        descr = "SpatialData object with:"
        attributes = ["images", "labels", "points", "polygons", "table"]
        for attr in attributes:
            attribute = getattr(self, attr)
            if attribute is not None and len(attribute) > 0:
                descr += f"\n{h('level0')}{attr}"
                if isinstance(attribute, AnnData):
                    descr += f"{h('empty_line')}"
                    descr_class = attribute.__class__.__name__
                    descr += f"{h('level1.0')}'{attribute}': {descr_class} {attribute.shape}"
                    descr = rreplace(descr, h("level1.0"), "    └── ", 1)
                else:
                    # descr = rreplace(descr, h("level0"), "└── ", 1)
                    for k, v in attribute.items():
                        descr += f"{h('empty_line')}"
                        descr_class = v.data.__class__.__name__
                        if attr == "points":
                            axes = ["x", "y", "z"][: v.ndim]
                            descr += (
                                f"{h(attr + 'level1.1')}'{k}': {descr_class} with osbm.spatial {v.shape}, "
                                f"with axes {', '.join(axes)}"
                            )
                        elif attr == "polygons":
                            # assuming 2d
                            axes = ["x", "y", "z"][: v.ndim]
                            descr += (
                                f"{h(attr + 'level1.1')}'{k}': {descr_class} with obs.spatial describing "
                                f"{len(v.data.obs)} polygons, with axes {', '.join(axes)}"
                            )
                        else:
                            assert attr in ["images", "labels"]
                            descr += (
                                f"{h(attr + 'level1.1')}'{k}': {descr_class} {v.shape}, with axes: "
                                f"{', '.join(v.data.dims)}"
                            )
                        # descr = rreplace(descr, h("level1.0"), "    └── ", 1)
            # the following lines go from this
            #     SpatialData object with:
            #     ├── Images
            #     │     └── 'image': DataArray (200, 100)
            #     └── Points
            #     │     ├── 'points': AnnData with osbm.spatial (50, 2)
            #     │     └── 'circles': AnnData with osbm.spatial (56, 2)
            # to this
            #     SpatialData object with:
            #     ├── Images
            #     │     └── 'image': DataArray (200, 100)
            #     └── Points
            #           ├── 'points': AnnData with osbm.spatial (50, 2)
            #           └── 'circles': AnnData with osbm.spatial (56, 2)
            latest_attribute_present = [
                attr
                for attr in attributes
                if getattr(self, attr) is not None and (attr == "table" or getattr(self, attr) != {})
            ][-1]
            if attr == latest_attribute_present:
                descr = descr.replace(h("empty_line"), "\n  ")
            else:
                descr = descr.replace(h("empty_line"), "\n│ ")

        descr = rreplace(descr, h("level0"), "└── ", 1)
        descr = descr.replace(h("level0"), "├── ")

        for attr in ["images", "labels", "points", "polygons", "table"]:
            descr = rreplace(descr, h(attr + "level1.1"), "    └── ", 1)
            descr = descr.replace(h(attr + "level1.1"), "    ├── ")
        ##
        descr += "\nwith coordinate systems:\n"
        for cs in self.coordinate_systems:
            descr += f"▸ {cs.name}\n" f'    with axes: {", ".join([axis.name for axis in cs.axes])}\n'
            gen = self._gen_spatial_elements()
            elements_in_cs = []
            for k, name, obj in gen:
                if cs.name in obj.coordinate_systems:
                    elements_in_cs.append(f"/{k}/{name}")
            if len(elements_in_cs) > 0:
                descr += f'    with elements: {", ".join(elements_in_cs)}\n'
        ##
        return descr


def _validate_axes(data: ArrayLike, axes: Tuple[str, ...]) -> Tuple[DataArray, Tuple[str, ...]]:
    """Reorder axes of data array.

    Parameters
    ----------
    data : ArrayLike
        Data array.
    axes : Tuple[str, ...]
        Axes of data array.
    axes_order : Tuple[str, ...]
        Desired order of axes.

    Returns
    -------
    ArrayLike
        Data array with reordered axes.
    """
    axes_order = ("t", "c", "z", "y", "x")
    sorted_axes = tuple(sorted(axes, key=lambda x: axes_order.index(x)))
    if sorted_axes == axes:
        return data, axes
    new_order = [sorted_axes.index(axis) for axis in axes]
    reverse = [new_order.index(a) for a in range(len(new_order))]
    transposed = data.transpose(*reverse)
    return transposed, tuple(sorted_axes)


def _validate_coordinate_systems(
    coordinate_systems: Optional[List[Union[CoordSystem_t, CoordinateSystem]]]
) -> Dict[str, CoordinateSystem]:
    validated = []
    for c in coordinate_systems:
        if isinstance(c, CoordinateSystem):
            validated.append(copy.deepcopy(c))
        # TODO: add type check, maybe with typeguard: https://stackoverflow.com/questions/51171908/extracting-data-from-typing-types
        # elif type(c) == CoordSystem_t:
        else:
            v = CoordinateSystem()
            v.from_dict(c)
            validated.append(v)
        # else:
        #     raise TypeError(f"Invalid type for coordinate system: {type(c)}")
    assert len(coordinate_systems) == len(validated)
    assert len(validated) == len(set(validated))
    d = {v.name: v for v in validated}
    assert len(d) == len(validated)
    return d


def _validate_transformations(
    elements_keys: List[str],
    prefix: str,
    transformations: Mapping[Tuple[str, str], Union[BaseTransformation, Dict[str, Any]]],
    coordinate_systems: Dict[str, CoordinateSystem],
) -> Dict[str, Dict[str, BaseTransformation]]:
    validated: Dict[str, Dict[str, BaseTransformation]] = {}
    for name in elements_keys:
        validated[f"/{prefix}/{name}"] = {}
    for (src, des), t in transformations.items():
        assert des in coordinate_systems.keys()
        if src.startswith(f"/{prefix}/"):
            src_name = src[len(f"/{prefix}/") :]
            if src_name in elements_keys:
                if isinstance(t, BaseTransformation):
                    v = copy.deepcopy(t)
                elif isinstance(t, dict):
                    # elif type(t) == Dict[str, Any]:
                    v = get_transformation_from_dict(t)
                else:
                    raise TypeError(f"Invalid type for transformation: {type(t)}")
                validated[src][des] = v
    return validated


if __name__ == "__main__":
    sdata = SpatialData.read("spatialdata-sandbox/merfish/data.zarr")
    s = sdata.polygons["anatomical"].data.obs.iloc[0]["spatial"]
    print(Polygons.string_to_tensor(s))
    print(sdata)
    print("ehi")
