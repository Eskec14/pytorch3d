# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.


"""This module implements utility functions for loading and saving meshes."""
import os
import warnings
from collections import namedtuple
from pathlib import Path
from typing import List, Optional, Union

import numpy as np
import torch
from iopath.common.file_io import PathManager
from pytorch3d.io.mtl_io import load_mtl, make_mesh_texture_atlas
from pytorch3d.io.utils import _check_faces_indices, _make_tensor, _open_file
from pytorch3d.renderer import TexturesAtlas, TexturesUV
from pytorch3d.structures import Meshes, join_meshes_as_batch

from .pluggable_formats import MeshFormatInterpreter, endswith


# Faces & Aux type returned from load_obj function.
_Faces = namedtuple("Faces", "verts_idx normals_idx textures_idx materials_idx")
_Aux = namedtuple(
    "Properties", "normals verts_uvs material_colors texture_images texture_atlas"
)


def _format_faces_indices(faces_indices, max_index, device, pad_value=None):
    """
    Format indices and check for invalid values. Indices can refer to
    values in one of the face properties: vertices, textures or normals.
    See comments of the load_obj function for more details.

    Args:
        faces_indices: List of ints of indices.
        max_index: Max index for the face property.
        pad_value: if any of the face_indices are padded, specify
            the value of the padding (e.g. -1). This is only used
            for texture indices indices where there might
            not be texture information for all the faces.

    Returns:
        faces_indices: List of ints of indices.

    Raises:
        ValueError if indices are not in a valid range.
    """
    faces_indices = _make_tensor(
        faces_indices, cols=3, dtype=torch.int64, device=device
    )

    if pad_value is not None:
        mask = faces_indices.eq(pad_value).all(dim=-1)

    # Change to 0 based indexing.
    faces_indices[(faces_indices > 0)] -= 1

    # Negative indexing counts from the end.
    faces_indices[(faces_indices < 0)] += max_index

    if pad_value is not None:
        faces_indices[mask] = pad_value

    return _check_faces_indices(faces_indices, max_index, pad_value)


def load_obj(
    f,
    load_textures=True,
    create_texture_atlas: bool = False,
    texture_atlas_size: int = 4,
    texture_wrap: Optional[str] = "repeat",
    device="cpu",
    path_manager: Optional[PathManager] = None,
):
    """
    Load a mesh from a .obj file and optionally textures from a .mtl file.
    Currently this handles verts, faces, vertex texture uv coordinates, normals,
    texture images and material reflectivity values.

    Note .obj files are 1-indexed. The tensors returned from this function
    are 0-indexed. OBJ spec reference: http://www.martinreddy.net/gfx/3d/OBJ.spec

    Example .obj file format:
    ::
        # this is a comment
        v 1.000000 -1.000000 -1.000000
        v 1.000000 -1.000000 1.000000
        v -1.000000 -1.000000 1.000000
        v -1.000000 -1.000000 -1.000000
        v 1.000000 1.000000 -1.000000
        vt 0.748573 0.750412
        vt 0.749279 0.501284
        vt 0.999110 0.501077
        vt 0.999455 0.750380
        vn 0.000000 0.000000 -1.000000
        vn -1.000000 -0.000000 -0.000000
        vn -0.000000 -0.000000 1.000000
        f 5/2/1 1/2/1 4/3/1
        f 5/1/1 4/3/1 2/4/1

    The first character of the line denotes the type of input:
    ::
        - v is a vertex
        - vt is the texture coordinate of one vertex
        - vn is the normal of one vertex
        - f is a face

    Faces are interpreted as follows:
    ::
        5/2/1 describes the first vertex of the first triangle
        - 5: index of vertex [1.000000 1.000000 -1.000000]
        - 2: index of texture coordinate [0.749279 0.501284]
        - 1: index of normal [0.000000 0.000000 -1.000000]

    If there are faces with more than 3 vertices
    they are subdivided into triangles. Polygonal faces are assumed to have
    vertices ordered counter-clockwise so the (right-handed) normal points
    out of the screen e.g. a proper rectangular face would be specified like this:
    ::
        0_________1
        |         |
        |         |
        3 ________2

    The face would be split into two triangles: (0, 2, 1) and (0, 3, 2),
    both of which are also oriented counter-clockwise and have normals
    pointing out of the screen.

    Args:
        f: A file-like object (with methods read, readline, tell, and seek),
           a pathlib path or a string containing a file name.
        load_textures: Boolean indicating whether material files are loaded
        create_texture_atlas: Bool, If True a per face texture map is created and
            a tensor `texture_atlas` is also returned in `aux`.
        texture_atlas_size: Int specifying the resolution of the texture map per face
            when `create_texture_atlas=True`. A (texture_size, texture_size, 3)
            map is created per face.
        texture_wrap: string, one of ["repeat", "clamp"]. This applies when computing
            the texture atlas.
            If `texture_mode="repeat"`, for uv values outside the range [0, 1] the integer part
            is ignored and a repeating pattern is formed.
            If `texture_mode="clamp"` the values are clamped to the range [0, 1].
            If None, then there is no transformation of the texture values.
        device: string or torch.device on which to return the new tensors.
        path_manager: optionally a PathManager object to interpret paths.

    Returns:
        6-element tuple containing

        - **verts**: FloatTensor of shape (V, 3).
        - **faces**: NamedTuple with fields:
            - verts_idx: LongTensor of vertex indices, shape (F, 3).
            - normals_idx: (optional) LongTensor of normal indices, shape (F, 3).
            - textures_idx: (optional) LongTensor of texture indices, shape (F, 3).
              This can be used to index into verts_uvs.
            - materials_idx: (optional) List of indices indicating which
              material the texture is derived from for each face.
              If there is no material for a face, the index is -1.
              This can be used to retrieve the corresponding values
              in material_colors/texture_images after they have been
              converted to tensors or Materials/Textures data
              structures - see textures.py and materials.py for
              more info.
        - **aux**: NamedTuple with fields:
            - normals: FloatTensor of shape (N, 3)
            - verts_uvs: FloatTensor of shape (T, 2), giving the uv coordinate per
              vertex. If a vertex is shared between two faces, it can have
              a different uv value for each instance. Therefore it is
              possible that the number of verts_uvs is greater than
              num verts i.e. T > V.
              vertex.
            - material_colors: if `load_textures=True` and the material has associated
              properties this will be a dict of material names and properties of the form:

              .. code-block:: python

                  {
                      material_name_1:  {
                          "ambient_color": tensor of shape (1, 3),
                          "diffuse_color": tensor of shape (1, 3),
                          "specular_color": tensor of shape (1, 3),
                          "shininess": tensor of shape (1)
                      },
                      material_name_2: {},
                      ...
                  }

              If a material does not have any properties it will have an
              empty dict. If `load_textures=False`, `material_colors` will None.

            - texture_images: if `load_textures=True` and the material has a texture map,
              this will be a dict of the form:

              .. code-block:: python

                  {
                      material_name_1: (H, W, 3) image,
                      ...
                  }
              If `load_textures=False`, `texture_images` will None.
            - texture_atlas: if `load_textures=True` and `create_texture_atlas=True`,
              this will be a FloatTensor of the form: (F, texture_size, textures_size, 3)
              If the material does not have a texture map, then all faces
              will have a uniform white texture.  Otherwise `texture_atlas` will be
              None.
    """
    data_dir = "./"
    # pyre-fixme[6]: Expected `Union[typing.Type[typing.Any],
    #  typing.Tuple[typing.Type[typing.Any], ...]]` for 2nd param but got `Any`.
    if isinstance(f, (str, bytes, os.PathLike)):
        # pyre-fixme[6]: Expected `_PathLike[Variable[typing.AnyStr <: [str,
        #  bytes]]]` for 1st param but got `Union[_PathLike[typing.Any], bytes, str]`.
        data_dir = os.path.dirname(f)
    if path_manager is None:
        path_manager = PathManager()
    with _open_file(f, path_manager, "r") as f:
        return _load_obj(
            f,
            data_dir=data_dir,
            load_textures=load_textures,
            create_texture_atlas=create_texture_atlas,
            texture_atlas_size=texture_atlas_size,
            texture_wrap=texture_wrap,
            path_manager=path_manager,
            device=device,
        )


def load_objs_as_meshes(
    files: list,
    device=None,
    load_textures: bool = True,
    create_texture_atlas: bool = False,
    texture_atlas_size: int = 4,
    texture_wrap: Optional[str] = "repeat",
    path_manager: Optional[PathManager] = None,
):
    """
    Load meshes from a list of .obj files using the load_obj function, and
    return them as a Meshes object. This only works for meshes which have a
    single texture image for the whole mesh. See the load_obj function for more
    details. material_colors and normals are not stored.

    Args:
        files: A list of file-like objects (with methods read, readline, tell,
            and seek), pathlib paths or strings containing file names.
        device: Desired device of returned Meshes. Default:
            uses the current device for the default tensor type.
        load_textures: Boolean indicating whether material files are loaded
        create_texture_atlas, texture_atlas_size, texture_wrap: as for load_obj.
        path_manager: optionally a PathManager object to interpret paths.

    Returns:
        New Meshes object.
    """
    mesh_list = []
    for f_obj in files:
        verts, faces, aux = load_obj(
            f_obj,
            load_textures=load_textures,
            create_texture_atlas=create_texture_atlas,
            texture_atlas_size=texture_atlas_size,
            texture_wrap=texture_wrap,
            path_manager=path_manager,
        )
        tex = None
        if create_texture_atlas:
            # TexturesAtlas type
            tex = TexturesAtlas(atlas=[aux.texture_atlas.to(device)])
        else:
            # TexturesUV type
            tex_maps = aux.texture_images
            if tex_maps is not None and len(tex_maps) > 0:
                verts_uvs = aux.verts_uvs.to(device)  # (V, 2)
                faces_uvs = faces.textures_idx.to(device)  # (F, 3)
                image = list(tex_maps.values())[0].to(device)[None]
                tex = TexturesUV(
                    verts_uvs=[verts_uvs], faces_uvs=[faces_uvs], maps=image
                )

        mesh = Meshes(
            verts=[verts.to(device)], faces=[faces.verts_idx.to(device)], textures=tex
        )
        mesh_list.append(mesh)
    if len(mesh_list) == 1:
        return mesh_list[0]
    return join_meshes_as_batch(mesh_list)


class MeshObjFormat(MeshFormatInterpreter):
    def __init__(self):
        self.known_suffixes = (".obj",)

    def read(
        self,
        path: Union[str, Path],
        include_textures: bool,
        device,
        path_manager: PathManager,
        create_texture_atlas: bool = False,
        texture_atlas_size: int = 4,
        texture_wrap: Optional[str] = "repeat",
        **kwargs,
    ) -> Optional[Meshes]:
        if not endswith(path, self.known_suffixes):
            return None
        mesh = load_objs_as_meshes(
            files=[path],
            device=device,
            load_textures=include_textures,
            create_texture_atlas=create_texture_atlas,
            texture_atlas_size=texture_atlas_size,
            texture_wrap=texture_wrap,
            path_manager=path_manager,
        )
        return mesh

    def save(
        self,
        data: Meshes,
        path: Union[str, Path],
        path_manager: PathManager,
        binary: Optional[bool],
        decimal_places: Optional[int] = None,
        **kwargs,
    ) -> bool:
        if not endswith(path, self.known_suffixes):
            return False

        verts = data.verts_list()[0]
        faces = data.faces_list()[0]
        save_obj(
            f=path,
            verts=verts,
            faces=faces,
            decimal_places=decimal_places,
            path_manager=path_manager,
        )
        return True


def _parse_face(
    line,
    tokens,
    material_idx,
    faces_verts_idx,
    faces_normals_idx,
    faces_textures_idx,
    faces_materials_idx,
):
    face = tokens[1:]
    face_list = [f.split("/") for f in face]
    face_verts = []
    face_normals = []
    face_textures = []

    for vert_props in face_list:
        # Vertex index.
        face_verts.append(int(vert_props[0]))
        if len(vert_props) > 1:
            if vert_props[1] != "":
                # Texture index is present e.g. f 4/1/1.
                face_textures.append(int(vert_props[1]))
            if len(vert_props) > 2:
                # Normal index present e.g. 4/1/1 or 4//1.
                face_normals.append(int(vert_props[2]))
            if len(vert_props) > 3:
                raise ValueError(
                    "Face vertices can only have 3 properties. \
                                Face vert %s, Line: %s"
                    % (str(vert_props), str(line))
                )

    # Triplets must be consistent for all vertices in a face e.g.
    # legal statement: f 4/1/1 3/2/1 2/1/1.
    # illegal statement: f 4/1/1 3//1 2//1.
    # If the face does not have normals or textures indices
    # fill with pad value = -1. This will ensure that
    # all the face index tensors will have F values where
    # F is the number of faces.
    if len(face_normals) > 0:
        if not (len(face_verts) == len(face_normals)):
            raise ValueError(
                "Face %s is an illegal statement. \
                        Vertex properties are inconsistent. Line: %s"
                % (str(face), str(line))
            )
    else:
        face_normals = [-1] * len(face_verts)  # Fill with -1
    if len(face_textures) > 0:
        if not (len(face_verts) == len(face_textures)):
            raise ValueError(
                "Face %s is an illegal statement. \
                        Vertex properties are inconsistent. Line: %s"
                % (str(face), str(line))
            )
    else:
        face_textures = [-1] * len(face_verts)  # Fill with -1

    # Subdivide faces with more than 3 vertices.
    # See comments of the load_obj function for more details.
    for i in range(len(face_verts) - 2):
        faces_verts_idx.append((face_verts[0], face_verts[i + 1], face_verts[i + 2]))
        faces_normals_idx.append(
            (face_normals[0], face_normals[i + 1], face_normals[i + 2])
        )
        faces_textures_idx.append(
            (face_textures[0], face_textures[i + 1], face_textures[i + 2])
        )
        faces_materials_idx.append(material_idx)


def _parse_obj(f, data_dir: str):
    """
    Load a mesh from a file-like object. See load_obj function for more details
    about the return values.
    """
    verts, normals, verts_uvs = [], [], []
    faces_verts_idx, faces_normals_idx, faces_textures_idx = [], [], []
    faces_materials_idx = []
    material_names = []
    mtl_path = None

    lines = [line.strip() for line in f]

    # startswith expects each line to be a string. If the file is read in as
    # bytes then first decode to strings.
    if lines and isinstance(lines[0], bytes):
        lines = [el.decode("utf-8") for el in lines]

    materials_idx = -1

    for line in lines:
        tokens = line.strip().split()
        if line.startswith("mtllib"):
            if len(tokens) < 2:
                raise ValueError("material file name is not specified")
            # NOTE: only allow one .mtl file per .obj.
            # Definitions for multiple materials can be included
            # in this one .mtl file.
            mtl_path = line[len(tokens[0]) :].strip()  # Take the remainder of the line
            mtl_path = os.path.join(data_dir, mtl_path)
        elif len(tokens) and tokens[0] == "usemtl":
            material_name = tokens[1]
            # materials are often repeated for different parts
            # of a mesh.
            if material_name not in material_names:
                material_names.append(material_name)
                materials_idx = len(material_names) - 1
            else:
                materials_idx = material_names.index(material_name)
        elif line.startswith("v "):  # Line is a vertex.
            vert = [float(x) for x in tokens[1:4]]
            if len(vert) != 3:
                msg = "Vertex %s does not have 3 values. Line: %s"
                raise ValueError(msg % (str(vert), str(line)))
            verts.append(vert)
        elif line.startswith("vt "):  # Line is a texture.
            tx = [float(x) for x in tokens[1:3]]
            if len(tx) != 2:
                raise ValueError(
                    "Texture %s does not have 2 values. Line: %s" % (str(tx), str(line))
                )
            verts_uvs.append(tx)
        elif line.startswith("vn "):  # Line is a normal.
            norm = [float(x) for x in tokens[1:4]]
            if len(norm) != 3:
                msg = "Normal %s does not have 3 values. Line: %s"
                raise ValueError(msg % (str(norm), str(line)))
            normals.append(norm)
        elif line.startswith("f "):  # Line is a face.
            # Update face properties info.
            _parse_face(
                line,
                tokens,
                materials_idx,
                faces_verts_idx,
                faces_normals_idx,
                faces_textures_idx,
                faces_materials_idx,
            )

    return (
        verts,
        normals,
        verts_uvs,
        faces_verts_idx,
        faces_normals_idx,
        faces_textures_idx,
        faces_materials_idx,
        material_names,
        mtl_path,
    )


def _load_materials(
    material_names: List[str],
    f,
    *,
    data_dir: str,
    load_textures: bool,
    device,
    path_manager: PathManager,
):
    """
    Load materials and optionally textures from the specified path.

    Args:
        material_names: a list of the material names found in the .obj file.
        f: a file-like object of the material information.
        data_dir: the directory where the material texture files are located.
        load_textures: whether textures should be loaded.
        device: string or torch.device on which to return the new tensors.
        path_manager: PathManager object to interpret paths.

    Returns:
        material_colors: dict of properties for each material.
        texture_images: dict of material names and texture images.
    """
    if not load_textures:
        return None, None

    if not material_names or f is None:
        if material_names:
            warnings.warn("No mtl file provided")
        return None, None

    if not os.path.isfile(f):
        warnings.warn(f"Mtl file does not exist: {f}")
        return None, None

    # Texture mode uv wrap
    return load_mtl(
        f,
        material_names=material_names,
        data_dir=data_dir,
        path_manager=path_manager,
        device=device,
    )


def _load_obj(
    f_obj,
    *,
    data_dir,
    load_textures: bool = True,
    create_texture_atlas: bool = False,
    texture_atlas_size: int = 4,
    texture_wrap: Optional[str] = "repeat",
    path_manager: PathManager,
    device="cpu",
):
    """
    Load a mesh from a file-like object. See load_obj function more details.
    Any material files associated with the obj are expected to be in the
    directory given by data_dir.
    """

    if texture_wrap is not None and texture_wrap not in ["repeat", "clamp"]:
        msg = "texture_wrap must be one of ['repeat', 'clamp'] or None, got %s"
        raise ValueError(msg % texture_wrap)

    (
        verts,
        normals,
        verts_uvs,
        faces_verts_idx,
        faces_normals_idx,
        faces_textures_idx,
        faces_materials_idx,
        material_names,
        mtl_path,
    ) = _parse_obj(f_obj, data_dir)

    verts = _make_tensor(verts, cols=3, dtype=torch.float32, device=device)  # (V, 3)
    normals = _make_tensor(
        normals, cols=3, dtype=torch.float32, device=device
    )  # (N, 3)
    verts_uvs = _make_tensor(
        verts_uvs, cols=2, dtype=torch.float32, device=device
    )  # (T, 2)

    faces_verts_idx = _format_faces_indices(
        faces_verts_idx, verts.shape[0], device=device
    )

    # Repeat for normals and textures if present.
    if len(faces_normals_idx):
        faces_normals_idx = _format_faces_indices(
            faces_normals_idx, normals.shape[0], device=device, pad_value=-1
        )
    if len(faces_textures_idx):
        faces_textures_idx = _format_faces_indices(
            faces_textures_idx, verts_uvs.shape[0], device=device, pad_value=-1
        )
    if len(faces_materials_idx):
        faces_materials_idx = torch.tensor(
            faces_materials_idx, dtype=torch.int64, device=device
        )

    texture_atlas = None
    material_colors, texture_images = _load_materials(
        material_names,
        mtl_path,
        data_dir=data_dir,
        load_textures=load_textures,
        path_manager=path_manager,
        device=device,
    )

    if create_texture_atlas:
        # Using the images and properties from the
        # material file make a per face texture map.

        # Create an array of strings of material names for each face.
        # If faces_materials_idx == -1 then that face doesn't have a material.
        idx = faces_materials_idx.cpu().numpy()
        face_material_names = np.array(material_names)[idx]  # (F,)
        face_material_names[idx == -1] = ""

        # Construct the atlas.
        texture_atlas = make_mesh_texture_atlas(
            material_colors,
            texture_images,
            face_material_names,
            faces_textures_idx,
            verts_uvs,
            texture_atlas_size,
            texture_wrap,
        )

    faces = _Faces(
        verts_idx=faces_verts_idx,
        normals_idx=faces_normals_idx,
        textures_idx=faces_textures_idx,
        materials_idx=faces_materials_idx,
    )
    aux = _Aux(
        normals=normals if len(normals) else None,
        verts_uvs=verts_uvs if len(verts_uvs) else None,
        material_colors=material_colors,
        texture_images=texture_images,
        texture_atlas=texture_atlas,
    )
    return verts, faces, aux


def save_obj(
    f,
    verts,
    faces,
    decimal_places: Optional[int] = None,
    path_manager: Optional[PathManager] = None,
):
    """
    Save a mesh to an .obj file.

    Args:
        f: File (or path) to which the mesh should be written.
        verts: FloatTensor of shape (V, 3) giving vertex coordinates.
        faces: LongTensor of shape (F, 3) giving faces.
        decimal_places: Number of decimal places for saving.
        path_manager: Optional PathManager for interpreting f if
            it is a str.
    """
    if len(verts) and not (verts.dim() == 2 and verts.size(1) == 3):
        message = "Argument 'verts' should either be empty or of shape (num_verts, 3)."
        raise ValueError(message)

    if len(faces) and not (faces.dim() == 2 and faces.size(1) == 3):
        message = "Argument 'faces' should either be empty or of shape (num_faces, 3)."
        raise ValueError(message)

    if path_manager is None:
        path_manager = PathManager()

    with _open_file(f, path_manager, "w") as f:
        return _save(f, verts, faces, decimal_places)


# TODO (nikhilar) Speed up this function.
def _save(f, verts, faces, decimal_places: Optional[int] = None) -> None:
    assert not len(verts) or (verts.dim() == 2 and verts.size(1) == 3)
    assert not len(faces) or (faces.dim() == 2 and faces.size(1) == 3)

    if not (len(verts) or len(faces)):
        warnings.warn("Empty 'verts' and 'faces' arguments provided")
        return

    verts, faces = verts.cpu(), faces.cpu()

    lines = ""

    if len(verts):
        if decimal_places is None:
            float_str = "%f"
        else:
            float_str = "%" + ".%df" % decimal_places

        V, D = verts.shape
        for i in range(V):
            vert = [float_str % verts[i, j] for j in range(D)]
            lines += "v %s\n" % " ".join(vert)

    if torch.any(faces >= verts.shape[0]) or torch.any(faces < 0):
        warnings.warn("Faces have invalid indices")

    if len(faces):
        F, P = faces.shape
        for i in range(F):
            face = ["%d" % (faces[i, j] + 1) for j in range(P)]
            if i + 1 < F:
                lines += "f %s\n" % " ".join(face)
            elif i + 1 == F:
                # No newline at the end of the file.
                lines += "f %s" % " ".join(face)

    f.write(lines)
