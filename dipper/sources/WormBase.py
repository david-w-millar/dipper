import csv
import re
import logging
import gzip
import io

from dipper.utils import pysed
from dipper.sources.Source import Source
from dipper.models.assoc.Association import Assoc
from dipper.models.Genotype import Genotype
from dipper.models.assoc.OrthologyAssoc import OrthologyAssoc
from dipper.models.Dataset import Dataset
from dipper.models.assoc.G2PAssoc import G2PAssoc
from dipper.models.Environment import Environment
from dipper.models.GenomicFeature import makeChromID
from dipper.models.GenomicFeature import Feature
from dipper.models.Reference import Reference
from dipper.utils.GraphUtils import GraphUtils
from dipper.models.GenomicFeature import Feature
from dipper import curie_map


logger = logging.getLogger(__name__)


class WormBase(Source):
    """
    This is the parser for the [C. elegans Model Organism Database (WormBase)](http://www.wormbase.org),
    from which we process genotype and phenotype data for laboratory worms (C.elegans and other nematodes).

    We generate the wormbase graph to include the following information:
    * genes
    * sequence alterations (includes SNPs/del/ins/indel and large chromosomal rearrangements)
    * RNAi as expression-affecting reagents
    * genotypes, and their components
    * strains
    * publications (and their mapping to PMIDs, if available)
    * allele-to-phenotype associations (including variants by RNAi)
    * genetic positional information for genes and sequence alterations

    Genotypes leverage the GENO genotype model and includes both intrinsic and extrinsic genotypes.  Where necessary,
    we create anonymous nodes of the genotype partonomy (such as for variant single locus complements,
    genomic variation complements, variant loci, extrinsic genotypes, and extrinsic genotype parts).
    """

    files = {
        'gene_ids': {'file': 'c_elegans.PRJNA13758.geneIDs.txt.gz',
                     'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/species/c_elegans/PRJNA13758/annotation/c_elegans.PRJNA13758.WS249.geneIDs.txt.gz'},
        'gene_desc': {'file': 'c_elegans.PRJNA13758.functional_descriptions.txt.gz',
                      'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/species/c_elegans/PRJNA13758/annotation/c_elegans.PRJNA13758.WS249.functional_descriptions.txt.gz'},
        'allele_pheno': {'file': 'phenotype_association.wb',
                         'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/ONTOLOGY/phenotype_association.WS249.wb'},
        'rnai_pheno': {'file': 'rnai_phenotypes.wb',
                       'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/ONTOLOGY/rnai_phenotypes.WS249.wb'},
        'pub_xrefs': {'file': 'pub_xrefs.txt',
                      'url': 'http://tazendra.caltech.edu/~azurebrd/cgi-bin/forms/generic.cgi?action=WpaXref'},
        'feature_loc': {'file': 'c_elegans.PRJNA13758.annotations.gff3.gz',
                        'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/species/c_elegans/PRJNA13758/c_elegans.PRJNA13758.WS249.annotations.gff3.gz'},
        'disease_assoc': {'file': 'disease_association.wb',
                          'url': 'ftp://ftp.sanger.ac.uk/pub/wormbase/releases/WS249/ONTOLOGY/disease_association.WS249.wb'},
        # 'genes_during_development': {'file': 'development_association.wb',
        #           'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/ONTOLOGY/development_association.WS249.wb'},
        # 'genes_in_anatomy': {'file': 'anatomy_association.wb',
        #           'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/ONTOLOGY/anatomy_association.WS249.wb'},
        # 'gene_interaction': {'file': 'c_elegans.PRJNA13758.gene_interactions.txt.gz',
        #                      'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/species/c_elegans/PRJNA13758/annotation/c_elegans.PRJNA13758.WS249.gene_interactions.txt.gz'},
        # 'orthologs': {'file': 'c_elegans.PRJNA13758.orthologs.txt.gz',
        #                     'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/species/c_elegans/PRJNA13758/annotation/c_elegans.PRJNA13758.WS249.orthologs.txt.gz'},
        'xrefs': {'file': 'c_elegans.PRJNA13758.xrefs.txt.gz',
                  'url': 'ftp://ftp.wormbase.org/pub/wormbase/releases/current-development-release/species/c_elegans/PRJNA13758/c_elegans.PRJNA13758.WS249.xrefs.txt.gz'},
    }

    test_ids = {
        'gene': ['WBGene00001414', 'WBGene00004967', 'WBGene00003916', 'WBGene00004397'],
        'allele': ['WBVar00087800', 'WBVar00087742', 'WBVar00144481', 'WBVar00248869'],
        'strain': ['BA794', 'RK1', 'HE1006'],
        'pub': []  # FIXME
    }


    def __init__(self):
        Source.__init__(self, 'wormbase')

        # update the dataset object with details about this resource
        # NO LICENSE for this resource
        self.dataset = Dataset('wormbase', 'WormBase', 'http://www.wormbase.org', None, None,
                               'http://www.wormbase.org/about/policies#012')

        return

    def fetch(self, is_dl_forced=False):

        # fetch all the files
        # TODO figure out the version number by probing the "current_release", then edit the file dict accordingly
        self.version_num = 'WS249'
        self.dataset.set_version_by_num(self.version_num)
        self.get_files(is_dl_forced)
        return

    def parse(self, limit=None):
        if limit is not None:
            logger.info("Only parsing first %s rows of each file", limit)
        logger.info("Parsing files...")

        if self.testOnly:
            self.testMode = True

        if self.testMode:
            g = self.testgraph
        else:
            g = self.graph

        self.nobnodes = True  # FIXME

        self.id_label_map = {}  # to hold any label for a given id
        self.genotype_backgrounds = {}  # to hold the mappings between genotype and background
        self.extrinsic_id_to_enviro_id_hash = {}
        self.variant_loci_genes = {}  # to hold the genes variant due to a seq alt
        self.environment_hash = {}  # to hold the parts of an environment
        self.wildtype_genotypes = []

        self.rnai_gene_map = {}  # stores the rnai_reagent to gene targets

        self.process_gene_ids(limit)
        self.process_gene_desc(limit)
        self.process_allele_phenotype(limit)
        self.process_rnai_phenotypes(limit)
        self.process_pub_xrefs(limit)
        self.process_feature_loc(limit)
        self.process_disease_association(limit)

        logger.info("Finished parsing.")

        self.load_bindings()
        gu = GraphUtils(curie_map.get())
        gu.loadAllProperties(g)
        gu.loadObjectProperties(g, Genotype.object_properties)

        logger.info("Found %d nodes in graph", len(self.graph))
        logger.info("Found %d nodes in testgraph", len(self.testgraph))

        return

    def process_gene_ids(self, limit):
        raw = '/'.join((self.rawdir, self.files['gene_ids']['file']))

        if self.testMode:
            g = self.testgraph
        else:
            g = self.graph

        gu = GraphUtils(curie_map.get())

        logger.info("Processing Gene IDs")
        line_counter = 0
        geno = Genotype(g)
        with gzip.open(raw, 'rb') as csvfile:
            filereader = csv.reader(io.TextIOWrapper(csvfile, newline=""), delimiter=',', quotechar='\"')
            for row in filereader:
                line_counter += 1
                (taxon_num, gene_num, gene_symbol, gene_synonym, live) = row
                # 6239,WBGene00000001,aap-1,Y110A7A.10,Live

                if self.testMode and gene_num not in self.test_ids['gene']:
                    continue

                taxon_id = 'NCBITaxon:'+taxon_num
                gene_id = 'WormBase:'+gene_num
                if gene_symbol == '':
                    gene_symbol = gene_synonym
                if gene_symbol == '':
                    gene_symbol = None
                gu.addClassToGraph(g, gene_id, gene_symbol, Genotype.genoparts['gene'])
                if live == 'Dead':
                    gu.addDeprecatedClass(g, gene_id)
                geno.addTaxon(taxon_id, gene_id)
                if gene_synonym != '':
                    gu.addSynonym(g, gene_id, gene_synonym)

                if not self.testMode and limit is not None and line_counter > limit:
                    break

        return


    def process_gene_desc(self, limit):
        raw = '/'.join((self.rawdir, self.files['gene_desc']['file']))

        if self.testMode:
            g = self.testgraph
        else:
            g = self.graph

        gu = GraphUtils(curie_map.get())

        logger.info("Processing Gene descriptions")
        line_counter = 0
        geno = Genotype(g)
        with gzip.open(raw, 'rb') as csvfile:
            filereader = csv.reader(io.TextIOWrapper(csvfile, newline=""), delimiter='\t', quotechar='\"')
            for row in filereader:
                if re.match('\#', ''.join(row)):
                    continue
                line_counter += 1
                if line_counter == 1:
                    continue
                (gene_num, public_name, molecular_name, concise_description, provisional_description,
                 detailed_description, automated_description, gene_class_description) = row

                if self.testMode and gene_num not in self.test_ids['gene']:
                    continue

                gene_id = 'WormBase:'+gene_num

                if concise_description != 'none available':
                    gu.addDefinition(g, gene_id, concise_description)

                # remove the description if it's identical to the concise
                descs = {
                    'provisional': provisional_description,
                    'automated': automated_description,
                    'detailed': detailed_description,
                    'gene class': gene_class_description
                }
                for d in descs:
                    text = descs.get(d)
                    if text == concise_description or re.match('none', text) or text == '':
                        pass  # don't use it
                    else:
                        text = ' '.join((text, '['+d+']'))
                        descs[d] = text
                        gu.addDescription(g, gene_id, text)

                if not self.testMode and limit is not None and line_counter > limit:
                    break

        return

    def process_allele_phenotype(self, limit=None):

        raw = '/'.join((self.rawdir, self.files['allele_pheno']['file']))

        if self.testMode:
            g = self.testgraph
        else:
            g = self.graph

        gu = GraphUtils(curie_map.get())

        logger.info("Processing Allele phenotype associations")
        line_counter = 0
        geno = Genotype(g)
        with open(raw, 'r') as csvfile:
            filereader = csv.reader(csvfile, delimiter='\t', quotechar='\"')
            for row in filereader:
                if re.match('!', ''.join(row)):  # header
                    continue
                line_counter += 1
                (db, gene_num, gene_symbol, is_not, phenotype_id, ref, eco_symbol, with_or_from, aspect, gene_name, gene_synonym, gene_class,
                taxon, date, assigned_by, blank, blank2) = row

                if self.testMode and gene_num not in self.test_ids['gene']:
                    continue

                # TODO add NOT phenotypes
                if is_not == 'NOT':
                    continue

                eco_id = None
                if eco_symbol == 'IMP':
                    eco_id = 'ECO:0000015'
                elif eco_symbol.strip() != '':
                    logger.warn("Encountered an ECO code we don't have: %s", eco_symbol)

                # something_with can be pipe delimited
                # WB:WBVar00095133|WB:WBVar00604230

                # there's some messiness in the file as of WS248.  some things in ref col are variants, others papers
                # and reciprocally, sometimes in the with column, sometimes they are variants.
                # here we clean them up

                temp_var = temp_ref = None
                if re.search('WBVar|WBRNAi', ref):
                    temp_var = ref
                    # move the paper from the with column into the ref
                if re.match('WBPerson', with_or_from):
                    temp_ref = with_or_from
                if temp_var is not None:
                    with_or_from = temp_var
                if temp_ref is not None:
                    ref = temp_ref
                # We assume that the allele-to-gene relationships are in another file?
                allele_list = re.split('\|', with_or_from)
                if len(allele_list) == 0:
                    logger.error("Missing alleles from phenotype assoc at line %d", line_counter)
                    continue
                elif len(allele_list) == 1:
                    allele_id = re.sub('WB:', 'WormBase:', allele_list[0])

                    if re.search('WBRNAi', allele_id):
                        # make the reagent-targeted gene, and annotate that instead of the RNAi item directly
                        rnai_num = re.sub('WormBase:', '', allele_id)
                        rnai_id = allele_id
                        gene_id = 'WormBase:'+gene_num
                        rtg_id = self._make_reagent_targeted_gene_id(gene_num, rnai_num)
                        geno.addReagentTargetedGene(rnai_id, 'WormBase:'+gene_num, rtg_id)
                        geno.addGeneTargetingReagent(rnai_id, None, geno.genoparts['RNAi_reagent'], gene_id)
                        allele_id = rtg_id
                    assoc = G2PAssoc(self.name, allele_id, phenotype_id)
                else:
                    # note there are never WBVars and RNAi reagents in the same row!  (how can that be?)
                    # build out a gvc-ish thing with the variant collection
                    allele_list = sorted(allele_list)
                    gvc_id = '_'+'-'.join(allele_list)
                    gvc_id = re.sub('WB:', '', gvc_id)
                    if self.nobnodes:
                        gvc_id = ':'+gvc_id
                    for a in allele_list:
                        a = re.sub('WB:', 'WormBase:', a)
                        geno.addParts(a, gvc_id, geno.object_properties['has_alternate_part'])
                    gu.addIndividualToGraph(g, gvc_id, None, geno.genoparts['genomic_variation_complement'])

                    assoc = G2PAssoc(self.name, gvc_id, phenotype_id)

                if eco_id is not None:
                    assoc.add_evidence(eco_id)

                if ref != '':
                    ref = re.sub('(WB:|WB_REF:)', 'WormBase:', ref)
                    assoc.add_source(ref)

                assoc.add_association_to_graph(g)

                if not self.testMode and limit is not None and line_counter > limit:
                    break

        return

    def process_rnai_phenotypes(self, limit=None):

        raw = '/'.join((self.rawdir, self.files['rnai_pheno']['file']))

        if self.testMode:
            g = self.testgraph
        else:
            g = self.graph

        gu = GraphUtils(curie_map.get())

        logger.info("Processing RNAi phenotype associations")
        line_counter = 0
        geno = Genotype(g)
        with open(raw, 'r') as csvfile:
            filereader = csv.reader(csvfile, delimiter='\t', quotechar='\"')
            for row in filereader:
                line_counter += 1
                (gene_num, gene_alt_symbol, phenotype_label, phenotype_id, rnai_and_refs) = row
# WBGene00001908	F17E9.9	locomotion variant	WBPhenotype:0000643	WBRNAi00025129|WBPaper00006395 WBRNAi00025631|WBPaper00006395
# WBGene00001908	F17E9.9	avoids bacterial lawn	WBPhenotype:0000402	WBRNAi00095640|WBPaper00040984
# WBGene00001908	F17E9.9	RAB-11 recycling endosome localization variant	WBPhenotype:0002107	WBRNAi00090830|WBPaper00041129

                if self.testMode and gene_num not in self.test_ids['gene']:
                    continue

                gene_id = 'WormBase:'+gene_num
                refs = list()

                # the rnai_and_refs has this so that
                # WBRNAi00008687|WBPaper00005654 WBRNAi00025197|WBPaper00006395 WBRNAi00045381|WBPaper00025054
                # space delimited between RNAi sets; then each RNAi should have a paper

                rnai_sets = re.split(' ', rnai_and_refs)

                for s in rnai_sets:

                    # get the rnai_id
                    (rnai_num, ref_num) = re.split('\|', s)
                    if rnai_num not in self.rnai_gene_map:
                        self.rnai_gene_map[rnai_num] = set()

                    self.rnai_gene_map[rnai_num].add(gene_num)  # to use for looking up later

                    rnai_id = 'WormBase:'+rnai_num
                    geno.addGeneTargetingReagent(rnai_id, None, geno.genoparts['RNAi_reagent'], gene_id)

                    #make the "allele" of the gene that is targeted by the reagent
                    allele_id = self._make_reagent_targeted_gene_id(gene_num, rnai_num)
                    allele_label = gene_alt_symbol+'<'+rnai_num+'>'
                    geno.addReagentTargetedGene(rnai_id, gene_id, allele_id, allele_label)

                    assoc = G2PAssoc(self.name, allele_id, phenotype_id)
                    assoc.add_source('WormBase:'+ref_num)
                    eco_id = 'ECO:0000019'  # RNAi evidence
                    assoc.add_association_to_graph(g)

                if not self.testMode and limit is not None and line_counter > limit:
                    break


        return

    def process_pub_xrefs(self, limit=None):

        raw = '/'.join((self.rawdir, self.files['pub_xrefs']['file']))

        if self.testMode:
            g = self.testgraph
        else:
            g = self.graph

        gu = GraphUtils(curie_map.get())

        logger.info("Processing publication xrefs")
        line_counter = 0
        with open(raw, 'r') as csvfile:
            filereader = csv.reader(csvfile, delimiter='\t', quotechar='\"')
            for row in filereader:
                line_counter += 1
                (wb_ref, xref) = row
                # WBPaper00000009 pmid8805<BR>
                # WBPaper00000011 doi10.1139/z78-244<BR>
                # WBPaper00000012 cgc12<BR>

                if self.testMode and wb_ref not in self.test_ids['pub']:
                    continue

                ref_id = 'WormBase:'+wb_ref
                xref_id = r = None
                xref = re.sub('<BR>','', xref)
                xref = xref.strip()
                if re.match('pmid', xref):
                    xref_id = 'PMID:'+re.sub('pmid\s*', '', xref)
                    r = Reference(xref_id, Reference.ref_types['journal_article'])
                elif re.search('[\(\)\<\>\[\]\s]', xref):
                    continue
                elif re.match('doi', xref):
                    xref_id = 'DOI:'+re.sub('doi', '', xref.strip())
                    r = Reference(xref_id)
                elif re.match('cgc', xref):
                    #TODO not sure what to do here with cgc xrefs
                    continue
                else:
                    # logger.debug("Other xrefs like %s", xref)
                    continue

                if xref_id is not None:
                    r.addRefToGraph(g)
                    gu.addSameIndividual(g, ref_id, xref_id)

                if not self.testMode and limit is not None and line_counter > limit:
                    break

        return


    def process_feature_loc(self, limit):

        raw = '/'.join((self.rawdir, self.files['feature_loc']['file']))

        if self.testMode:
            g = self.testgraph
        else:
            g = self.graph

        gu = GraphUtils(curie_map.get())

        logger.info("Processing Feature location and attributes")
        line_counter = 0
        geno = Genotype(g)
        strain_to_variant_map = {}
        with gzip.open(raw, 'rb') as csvfile:
            filereader = csv.reader(io.TextIOWrapper(csvfile, newline=""), delimiter='\t', quotechar='\"')
            for row in filereader:
                if re.match('\#', ''.join(row)):
                    continue
                (chrom, db, feature_type_label, start, end, score, strand, phase, attributes) = row

# I	interpolated_pmap_position	gene	1	559768	.	.	.	ID=gmap:spe-13;gmap=spe-13;status=uncloned;Note=-21.3602 cM (+/- 1.84 cM)
# I	WormBase	gene	3747	3909	.	-	.	ID=Gene:WBGene00023193;Name=WBGene00023193;interpolated_map_position=-21.9064;sequence_name=Y74C9A.6;biotype=snoRNA;Alias=Y74C9A.6
# I	absolute_pmap_position	gene	4119	10230	.	.	.	ID=gmap:homt-1;gmap=homt-1;status=cloned;Note=-21.8252 cM (+/- 0.00 cM)

                # dbs = re.split(' ', 'assembly_component expressed_sequence_match Coding_transcript Genomic_canonical Non_coding_transcript Orfeome Promoterome Pseudogene RNAi_primary RNAi_secondary Reference Transposon Transposon_CDS cDNA_for_RNAi miRanda ncRNA operon polyA_signal_sequence polyA_site snlRNA')
                #
                # if db not in dbs:
                #     continue

                if feature_type_label not in ['gene', 'point_mutation', 'deletion', 'RNAi_reagent',
                                          'duplication', 'enhancer', 'binding_site', 'biological_region',
                                          'complex_substitution']:
                    # note biological_regions include balancers
                    continue
                line_counter += 1

                attribute_dict = {}
                if attributes != '':
                    attribute_dict = dict(item.split("=") for item in re.sub('"', '', attributes).split(";"))

                fid = flabel = desc = None
                if 'ID' in attribute_dict:
                    fid = attribute_dict.get('ID')
                    if re.search('WB(Gene|Var|sf)', fid):
                        fid = re.sub('^\w+:WB', 'WormBase:WB', fid)
                    elif re.match('(gmap|landmark)', fid):
                        continue
                    else:
                        logger.info('other identifier %s', fid)
                        fid = None
                elif 'variation' in attribute_dict:
                    fid = 'WormBase:'+attribute_dict.get('variation')
                    flabel = attribute_dict.get('public_name')
                    sub = attribute_dict.get('substitution')
                    ins = attribute_dict.get('insertion')
                    # if it's a variation:
                    # variation=WBVar00604246;public_name=gk320600;strain=VC20384;substitution=C/T
                    desc = ''
                    if sub is not None:
                        desc = 'substitution='+sub
                    if ins is not None:
                        desc = 'insertion='+ins

                    # keep track of the strains with this variation, for later processing
                    strain_list = attribute_dict.get('strain')
                    if strain_list is not None:
                        for s in re.split(',', strain_list):
                            if s.strip() not in strain_to_variant_map:
                                strain_to_variant_map[s.strip()] = set()
                            strain_to_variant_map[s.strip()].add(fid)

                if feature_type_label == 'RNAi_reagent':
                    # Target=WBRNAi00096030 1 4942
                    # this will tell us where the RNAi is actually binding
                    target = attribute_dict.get('Target')
                    rnai_num = re.split(' ', target)[0]
                    # it will be the reagent-targeted-gene that has a position, i think
                    # TODO finish the RNAi binding location

                name = attribute_dict.get('Name')
                polymorphism = attribute_dict.get('polymorphism')

                if fid is None:
                    if name is not None and re.match('WBsf', name):
                        fid = 'WormBase:'+name
                        name = None
                    else:
                        continue

                if self.testMode and re.sub('WormBase:', '', fid) not in self.test_ids['gene']+self.test_ids['allele']:
                    continue

                if polymorphism is not None:  # these really aren't that interesting
                    continue

                if name is not None and not re.search(name, fid):
                    if flabel is None:
                        flabel = name
                    else:
                        gu.addSynonym(g, fid, name)

                if desc is not None:
                    gu.addDescription(g, fid, desc)

                alias = attribute_dict.get('Alias')

                biotype = attribute_dict.get('biotype')
                note = attribute_dict.get('Note')
                other_name = attribute_dict.get('other_name')
                for n in [alias, other_name]:
                    if n is not None:
                        gu.addSynonym(g, fid, other_name)

                ftype = self.get_feature_type_by_class_and_biotype(feature_type_label, biotype)

                build_num = 'WS249'
                build_id = 'WormBase:'+build_num
                chr_id = makeChromID(chrom, build_id, 'CHR')   # HARDCODE - FIXME
                geno.addChromosomeInstance(chrom, build_id, build_num)

                f  = Feature(fid, flabel, ftype)
                f.addFeatureStartLocation(start, chr_id, strand)
                f.addFeatureEndLocation(start, chr_id, strand)

                feature_is_class = False
                if feature_type_label == 'gene':
                    feature_is_class = True

                f.addFeatureToGraph(g, True, None, feature_is_class)

                if note is not None:
                    gu.addDescription(g, fid, note)

                if not self.testMode and limit is not None and line_counter > limit:
                    break

                # RNAi reagents:
# I	RNAi_primary	RNAi_reagent	4184	10232	.	+	.	Target=WBRNAi00001601 1 6049 +;laboratory=YK;history_name=SA:yk326e10
# I	RNAi_primary	RNAi_reagent	4223	10147	.	+	.	Target=WBRNAi00033465 1 5925 +;laboratory=SV;history_name=MV_SV:mv_G_YK5052
# I	RNAi_primary	RNAi_reagent	5693	9391	.	+	.	Target=WBRNAi00066135 1 3699 +;laboratory=CH

                # TODO TF bindiing sites and network:
# I	TF_binding_site_region	TF_binding_site	1861	2048	.	+	.	Name=WBsf292777;tf_id=WBTranscriptionFactor000025;tf_name=DAF-16
# I	TF_binding_site_region	TF_binding_site	3403	4072	.	+	.	Name=WBsf331847;tf_id=WBTranscriptionFactor000703;tf_name=DPL-1

        return

    def process_disease_association(self, limit):


        raw = '/'.join((self.rawdir, self.files['disease_assoc']['file']))

        if self.testMode:
            g = self.testgraph
        else:
            g = self.graph

        gu = GraphUtils(curie_map.get())

        logger.info("Processing disease models")
        line_counter = 0
        with open(raw, 'r') as csvfile:
            filereader = csv.reader(csvfile, delimiter='\t', quotechar='\"')
            for row in filereader:
                if re.match('!', ''.join(row)):  # header
                    continue
                line_counter += 1
                (db, gene_num, gene_symbol, is_not, disease_id, ref, eco_symbol, with_or_from, aspect, gene_name, gene_synonym, gene_class,
                taxon, date, assigned_by, blank, blank2) = row

                if self.testMode and gene_num not in self.test_ids['gene']:
                    continue

                # TODO add NOT phenotypes
                if is_not == 'NOT':
                    continue

                # WB	WBGene00000001	aap-1		DOID:2583	PMID:19029536	IEA	ENSEMBL:ENSG00000145675|OMIM:615214	D		Y110A7A.10	gene	taxon:6239	20150612	WB
                gene_id = 'WormBase:'+gene_num
                assoc = G2PAssoc(self.name, gene_id, disease_id, gu.object_properties['model_of'])
                ref = re.sub('WB_REF:', 'WormBase:', ref)
                if ref != '':
                    assoc.add_source(ref)
                if eco_symbol == 'IEA':
                    eco_id = 'ECO:0000501'  # IEA is this now
                if eco_id is not None:
                    assoc.add_evidence(eco_id)

                assoc.add_association_to_graph(g)

        return

    def get_feature_type_by_class_and_biotype(self, ftype, biotype):
        ftype_id = None
        biotype_map = {
            'lincRNA': 'SO:0001641',
            'miRNA': 'SO:0001265',
            'ncRNA': 'SO:0001263',
            'piRNA': 'SO:0001638',
            'rRNA': 'SO:0001637',
            'scRNA': 'SO:0001266',
            'snRNA': 'SO:0001268',
            'snoRNA': 'SO:0001267',
            'tRNA': 'SO:0001272',
            'transposon_protein_coding': 'SO:0000111',  # transposable element gene
            'transposon_pseudogene': 'SO:0001897',
            'pseudogene': 'SO:0000336',
            'protein_coding': 'SO:0001217',
            'asRNA': 'SO:0001263',  # using ncRNA gene  TODO make term request
        }

        ftype_map = {
            'point_mutation': 'SO:1000008',
            'deletion': 'SO:0000159',
            'RNAi_reagent': 'SO:0000337',
            'duplication': 'SO:1000035',
            'enhancer': 'SO:0000165',
            'binding_site': 'SO:0000409',
            'biological_region': 'SO:0001411',
            'complex_substitution': 'SO:1000005'
        }
        if ftype == 'gene':
            if biotype in biotype_map:
                ftype_id = biotype_map.get(biotype)
        else:
            ftype_id = ftype_map.get(ftype)

        return ftype_id

    def _make_reagent_targeted_gene_id(self, gene_id, reagent_id):

        rtg_id = '_'+'-'.join((gene_id, reagent_id))
        if self.nobnodes:
            rtg_id = ':'+rtg_id

        return rtg_id

    def getTestSuite(self):
        import unittest
        from tests.test_wormbase import WormBaseTestCase

        test_suite = unittest.TestLoader().loadTestsFromTestCase(WormBaseTestCase)

        return test_suite