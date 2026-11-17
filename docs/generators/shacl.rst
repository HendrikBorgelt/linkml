SHACL
======

.. warning:: Beta implementation, some features may change

Example Output
--------------

`personinfo.shacl.ttl <https://github.com/linkml/linkml/tree/main/examples/PersonSchema/personinfo/shacl/personinfo.shacl.ttl>`_

Overview
--------

`SHACL <https://www.w3.org/TR/shacl/>`__ (Shapes Constraint Language) is a language for validating RDF graphs against a set of conditions

To run:

.. code:: bash

   gen-shacl personinfo.yaml > personinfo.shacl.ttl



Docs
----

Example Input:

.. code-block:: yaml

  NamedThing:
    slots:
      - id
      - name

  HasAliases:
    mixin: true
    attributes:
      aliases:
        multivalued: true

  Person:
    is_a: NamedThing
    mixins:
      - HasAliases
    slots:
      - birth_date
      - age_in_years
      - gender

Example Output:

.. code-block:: turtle

    <https://w3id.org/linkml/tests/kitchen_sink/Person> a shacl:NodeShape ;
        shacl:closed true ;
        shacl:ignoredProperties ( rdf:type ) ;
        shacl:property [ shacl:class <https://w3id.org/linkml/tests/kitchen_sink/BirthEvent> ;
                shacl:maxCount 1 ;
                shacl:nodeKind shacl:BlankNode ;
                shacl:path <https://w3id.org/linkml/tests/kitchen_sink/has_birth_event> ],
            [ shacl:maxCount 1 ;
                shacl:maxInclusive 999 ;
                shacl:minInclusive 0 ;
                shacl:path <https://w3id.org/linkml/tests/kitchen_sink/age_in_years> ],
            [ shacl:class <https://w3id.org/linkml/tests/kitchen_sink/FamilialRelationship> ;
                shacl:nodeKind shacl:BlankNode ;
                shacl:path <https://w3id.org/linkml/tests/kitchen_sink/has_familial_relationships> ],
            [ shacl:maxCount 1 ;
                shacl:path <https://w3id.org/linkml/tests/core/name> ;
                shacl:pattern "^\\S+ \\S+" ],
            [ shacl:class <https://w3id.org/linkml/tests/kitchen_sink/MedicalEvent> ;
                shacl:nodeKind shacl:BlankNode ;
                shacl:path <https://w3id.org/linkml/tests/kitchen_sink/has_medical_history> ],
            [ shacl:class <https://w3id.org/linkml/tests/kitchen_sink/Address> ;
                shacl:nodeKind shacl:BlankNode ;
                shacl:path <https://w3id.org/linkml/tests/kitchen_sink/addresses> ],
            [ shacl:maxCount 1 ;
                shacl:path <https://w3id.org/linkml/tests/core/id> ],
            [ shacl:path <https://w3id.org/linkml/tests/kitchen_sink/aliases> ],
            [ shacl:class <https://w3id.org/linkml/tests/kitchen_sink/EmploymentEvent> ;
                shacl:nodeKind shacl:BlankNode ;
                shacl:path <https://w3id.org/linkml/tests/kitchen_sink/has_employment_history> ] ;
        shacl:targetClass <https://w3id.org/linkml/tests/kitchen_sink/Person> .


Closed and open shapes
^^^^^^^^^^^^^^^^^^^^^^

A closed shape (``sh:closed true``) rejects every property it does not declare. By default every class shape is
closed, except those of mixins and abstract classes; ``--non-closed`` makes them all open. A class can decide for
itself with ``extra_slots``, which takes precedence over ``--closed``/``--non-closed``, as it does in the JSON
Schema generator:

.. list-table::
   :header-rows: 1

   * - ``extra_slots`` on the class
     - Shape
   * - ``allowed: true``
     - open
   * - ``allowed: false``
     - closed, also with ``--non-closed``
   * - ``{}`` (neither ``allowed`` nor ``range_expression``)
     - closed, also with ``--non-closed``
   * - ``range_expression: ...`` (without ``allowed``)
     - open, with a warning that the expression is not enforced
   * - not set
     - closed with ``--closed`` (the default), open with ``--non-closed``; mixins and abstract classes open

SHACL Core can constrain the values of a given property, but not those of "every property the shape does not
declare", so a ``range_expression`` is not enforced. A SHACL-SPARQL constraint (``sh:sparql``) could check it; the
generator does not produce one yet.

``extra_slots`` is not inherited: a subclass without ``extra_slots`` follows ``--closed``/``--non-closed``, even
if its parent allows extra slots. The metamodel leaves inheritance open, see
`#3887 <https://github.com/linkml/linkml/issues/3887>`_ and `#4145 <https://github.com/linkml/linkml/issues/4145>`_.
Opening a subclass does not open its parent either: a node that is also checked against a closed parent shape
(through ``sh:node`` with ``--use-native-names``, or with RDFS inference) is still rejected for undeclared
properties.

Closed shapes get ``sh:ignoredProperties``: ``rdf:type`` and the properties of the class's descendants, so that
instances of a subclass, or of a class using a mixin, pass the shape. Open shapes get none, as it would have no
effect (`#3539 <https://github.com/linkml/linkml/issues/3539>`_). Open shapes say ``sh:closed false`` only with
``--closed``, where it marks them as open on purpose; with ``--non-closed`` they carry no ``sh:closed`` at all,
as SHACL shapes are open by default.

Example: with this schema, ``Note`` shapes reject undeclared properties even with ``--non-closed``, while
``Annotation`` shapes accept them even with ``--closed``.

.. code-block:: yaml

  classes:
    Note:
      slots: [text]
      extra_slots:
        allowed: false
    Annotation:
      slots: [text]
      extra_slots:
        allowed: true


Classes and slots that share a URI
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

LinkML tells classes and slots apart by name, but RDF only sees their URIs. When several classes share a
``class_uri``, or several slots of a class share a ``slot_uri``, a validator cannot tell which of them a node
or a triple belongs to. The generator then emits shapes that hold for all of them at once, and logs a warning
because this is often not intended:

* **Slots sharing a slot_uri** become a single property shape with ``sh:or`` over their ranges. Each slot keeps
  its own cardinality as a qualified count (``sh:qualifiedValueShape``), and its own value constraints such as
  ``pattern``. This applies with both naming modes. If one of the slots only accepts values another one accepts
  anyway (e.g. a subclass with the same ``class_uri`` as its parent), a warning says that its constraints never
  reject a value.
* **Classes sharing a class_uri** are emitted into a single shape with ``--use-class-uri-names`` (the default).
  With ``--use-native-names`` every class keeps its own shape, but only the class without a parent in the group
  keeps ``sh:targetClass``; the others become *role shapes*, checked where a slot's range names them. A group of
  unrelated classes, or one in which a subclass relaxes its parent (through ``slot_usage``, or by allowing extra
  slots while the parent's shape is closed), gets a *role union shape* (``<name>Roles``) instead: it targets the
  ``class_uri`` and accepts a node conforming to any of the classes. A role union shape is always open, and the
  single shape of ``--use-class-uri-names`` is closed only if every one of its classes is.
* **Slots on rdf:type** accept the node's own class assertion besides their range, and do not count it.

To get full validation for each element, give them distinct URIs. See :class:`ShaclGenerator` below for the
details.


Command Line
^^^^^^^^^^^^

.. currentmodule:: linkml.generators.shaclgen

.. click:: linkml.generators.shaclgen:cli
    :prog: gen-shacl
    :nested: short

Code
^^^^


.. autoclass:: ShaclGenerator
    :members: serialize
