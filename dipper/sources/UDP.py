from dipper.sources.Source import Source
from dipper import curie_map
from dipper.models.Genotype import Genotype
from dipper.utils.GraphUtils import GraphUtils
from dipper.models.Dataset import Dataset
import logging
import csv
import re
import os

logger = logging.getLogger(__name__)


class UDP(Source):
    """
    The National Institutes of Health (NIH) Undiagnosed Diseases Program (UDP)
    is part of the Undiagnosed Disease Network (UDN),
    an NIH Common Fund initiative that focuses on the most puzzling medical cases
    referred to the NIH Clinical Center in Bethesda, Maryland.
    from https://www.genome.gov/27544402/the-undiagnosed-diseases-program/

    Data is available by request for access via the NHGRI collaboration server:
    https://udplims-collab.nhgri.nih.gov/api

    Note this source class does not include a fetch method since the data is private
    The parser works generically when two tsv files are present in the raw directory
    /raw/udp with the structure

    udp_variants.tsv
    'Patient', 'Family', 'Chr', 'Build', 'Chromosome Position',
    'Reference Allele', 'Variant Allele', 'Parent of origin',
    'Allele Type', 'Mutation Type', 'Gene', 'Transcript', 'Original Amino Acid',
    'Variant Amino Acid', 'Amino Acid Change', 'Segregates with',
    'Position', 'Exon', 'Inheritance model', 'Zygosity', 'dbSNP ID', '1K Frequency',
    'Number of Alleles'

    udp_phenotypes.tsv
    'Patient', 'HPID', 'Present'

    The script also utilizes two mapping files
    udp_gene_map.tsv -  generated from scripts/fetch-gene-ids.py,
                        gene symbols from udp_variants
    udp_chr_rs.tsv - rsid(s) per coordinate greped from hg19 dbsnp file,
                     then disambiguated with eutils, see scripts/dbsnp/dbsnp.py

    """
    files = {
        'patient_phenotypes': {
            'file': 'udp_phenotypes.tsv'
        },
        'patient_variants': {
            'file': 'udp_variants.tsv'
        }
    }
    map_files = {
        'gene_map': '../../resources/udp/udp_gene_map.tsv',
        'dbsnp_map': '../../resources/udp/udp_chr_rs.tsv',
        'gene_coord_map': '../../resources/udp/gene_coordinates.tsv'
    }

    def __init__(self):
        super().__init__('udp')
        self.dataset = Dataset(
            'udp', 'UDP', 'https://rarediseases.info.nih.gov/')

    def parse(self, limit=None):
        """
        Override Source.parse()
        Args:
            :param limit (int, optional) limit the number of rows processed
        Returns:
            :return None
        """
        self.load_bindings()
        if limit is not None:
            logger.info("Only parsing first %d rows", limit)

        phenotype_file = '/'.join((self.rawdir, self.files['patient_phenotypes']['file']))
        variant_file = '/'.join((self.rawdir, self.files['patient_variants']['file']))

        self._parse_patient_phenotypes(phenotype_file, limit)
        self._parse_patient_variants(variant_file, limit)

        return

    def _parse_patient_variants(self, file, limit):
        """
        :param file: file path
        :param limit: limit (int, optional) limit the number of rows processed (NOT IMPLEMENTED)
        :return:
        """
        patient_var_map = self._convert_variant_file_to_dict(file)
        gene_id_map = self.parse_mapping_file(self.map_files['gene_map'])
        gene_coordinate_map = self._parse_gene_coordinates(self.map_files['gene_coord_map'])

        genotype_util = Genotype(self.graph)
        graph_util = GraphUtils(curie_map.get())

        for patient in patient_var_map:
            patient_curie = ':{0}'.format(patient)
            # make intrinsic genotype for each patient
            intrinsic_geno_bnode = self.make_id("{0}-intrinsic-genotype".format(patient), "_")
            genotype_label = "{0} genotype".format(patient)
            genotype_util.addGenotype(intrinsic_geno_bnode,
                                      genotype_label,
                                      genotype_util.genoparts['intrinsic_genotype'])

            graph_util.addTriple(self.graph, patient_curie,
                                 genotype_util.object_properties['has_genotype'],
                                 intrinsic_geno_bnode)

        return

    def _add_variant_gene_relationship(self, ):
        """
        Right now it is unclear the best approach on how to connect
        variants to genes.  In most cases has_affected_locus/GENO:0000418
        is accurate; however, there are cases where a variant is in the intron
        on one gene and is purported to causally affect another gene down or
        upstream.  In these cases we must first disambiguate which gene
        is the affected locus, and which gene(s) are predicated to be
        causully influenced by (RO:0002566)

        The logic followed here is:
        if mutation type contains downstream/upstream and more than one
        gene of interest, investigate coordinates of all genes to
        see if we can disambiguate which genes are which
        :return:
        """
        return

    def _convert_variant_file_to_dict(self, file):
        """
        Converts tsv to dicts with this structure
        {
            'patient_1': {
                'variant-id': {
                    'build': hg19
                    'chromosome': 'chr7',
                    'reference_allele': 'A',
                    'variant_allele': 'G',
                    'rs_id' : 'RS1234',
                    'type': 'SNV",
                    'genes_of_interest' : [SHH, BRCA1]
                }
            }
        }
        If any part of the core variant information is missing
        (build, chr, bp change(s), the line number will be used
        to make the variant unique

        Variant id will be used downstream to form blank nodes (checksumed)

        Values are normalized with these rules:
        1. Basepairs are upper case
        2. HG19 -> hg19
        3. X -> chrX
        :return: dict
        """
        patient_variant_map = {}
        line_num = 0
        with open(file, 'rt') as tsvfile:
            reader = csv.reader(tsvfile, delimiter="\t")
            for row in reader:

                (patient, family, chromosome, build, position,
                 reference_allele, variant_allele, parent_of_origin,
                 allele_type, mutation_type, gene_symbol, transcript,
                 reference_aa, variant_aa, aa_change, segregates_with,
                 locus, exon, inheritance_model, zygosity, dbSNP_ID, frequency,
                 num_of_alleles) = row

                if patient not in patient_variant_map:
                    patient_variant_map[patient] = {}

                formatted_chr = re.sub(r'^CHR', 'chr', chromosome, flags=re.I)

                if re.match(r'[XY]|[1-9]{1,2}', chromosome, flags=re.I):
                    formatted_chr = "chr{0}".format(chromosome.upper())

                formatted_build = re.sub(r'^HG', 'hg', build, flags=re.I)
                ref_base = reference_allele.upper()
                var_base = variant_allele.upper()
                rs_id = ''

                # Catch misformatted data
                if re.search(r'LEFT FLANK|NM_|EXON', ref_base):
                    ref_base = ''

                if re.search(r'LEFT FLANK|NM_|EXON', var_base):
                    var_base = ''

                if re.search(r'chrGL', formatted_chr):
                    formatted_chr = ''

                if dbSNP_ID != '':
                    match = re.match(r'^(rs\d+).*', dbSNP_ID)
                    if match:
                        rs_id = match.group(1)

                # Format variant object
                variant_info = [formatted_chr, formatted_build, position,
                                ref_base, var_base]

                if '' in variant_info:
                    filt_list = [info for info in variant_info if info != '']
                    variant_id = str(line_num) + '-' + '-'.join(filt_list)
                else:
                    variant_id = '-'.join(variant_info)

                if variant_id in patient_variant_map[patient]:
                    patient_variant_map[patient][variant_id]['genes_of_interest'].append(gene_symbol)
                else:
                    patient_variant_map[patient][variant_id] = {
                        'build': formatted_build,
                        'chromosome': formatted_chr,
                        'reference_allele': ref_base,
                        'variant_allele': var_base,
                        'type': mutation_type
                    }
                    if rs_id:
                        patient_variant_map[patient][variant_id]['rs_id'] = rs_id

                    patient_variant_map[patient][variant_id]['genes_of_interest'] = [gene_symbol]

                line_num += 1

        return patient_variant_map

    def _parse_patient_phenotypes(self, file, limit):
        """
        :param file: file path
        :param limit: limit (int, optional) limit the number of rows processed
        :return:
        """
        genotype_util = Genotype(self.graph)
        graph_util = GraphUtils(curie_map.get())
        line_counter = 0
        with open(file, 'r') as tsvfile:
            reader = csv.reader(tsvfile, delimiter="\t")
            for row in reader:
                (patient_id, hpo_curie, present) = row
                patient_curie = ':{0}'.format(patient_id)
                graph_util.addPerson(self.graph, patient_curie, patient_id)

                graph_util.addTriple(self.graph, patient_curie,
                                     graph_util.object_properties['has_phenotype'],
                                     "DOID:4")
                if present == 'yes':
                    graph_util.addTriple(self.graph, patient_curie,
                                         graph_util.object_properties['has_phenotype'],
                                         hpo_curie)

                line_counter += 1
                if not self.testMode and limit is not None \
                        and line_counter >= limit:
                    break

    @staticmethod
    def _parse_gene_coordinates(file):
        """
        :param file: file path
        :param limit: limit (int, optional) limit the number of rows processed
        :return: dict
        """
        id_map = {}
        if os.path.exists(os.path.join(os.path.dirname(__file__), file)):
            with open(os.path.join(os.path.dirname(__file__), file)) as tsvfile:
                reader = csv.reader(tsvfile, delimiter="\t")
                for row in reader:
                    (gene_curie, start, end, strand, build) = row
                    id_map[gene_curie] = {
                        'start': start,
                        'end': end,
                        'strand': strand,
                        'build': build
                    }
        return id_map
