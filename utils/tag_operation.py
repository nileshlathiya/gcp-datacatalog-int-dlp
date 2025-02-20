from utils.utils import read_json, read_tag_csv, prepare_dict
from utils.gcs_operation import list_file_gcs, download_file_gcs, move_file_gcs, upload_file_to_gcs
from utils.tmpl_operation import get_template, get_latest_template_id, get_all_latest_template_id
import os, csv
from google.cloud import datacatalog
from utils.policy_tag_operation import auto_attach_policy_tag

def get_entry(project, dataset, table):
    # retrieve a project.dataset.table entry
    datacatalog_client = datacatalog.DataCatalogClient()
    resource_name = f"//bigquery.googleapis.com/projects/{project}"
    if dataset != "":
        resource_name = resource_name + f"/datasets/{dataset}"
    if table != "":
        resource_name = resource_name + f"/tables/{table}"

    try:
        table_entry = datacatalog_client.lookup_entry(request={"linked_resource": resource_name})
        return table_entry.name
    except Exception:
        return ""

def remove_tag(table_entry, project, template, template_location, column_name=""):
    # remove tag for a table

    # list the tag related with table
    datacatalog_client = datacatalog.DataCatalogClient()
    request = datacatalog.ListTagsRequest()
    request.parent = table_entry
    gdc_tag_result = datacatalog_client.list_tags(request=request)
    related_template = f"projects/{project}/locations/{template_location}/tagTemplates/{template}"
    already_exist = False
    tag_entry_name = ""

    for tag in gdc_tag_result.tags:
        if column_name == "":
            if tag.template == related_template and tag.column == "":
                already_exist = True
                tag_entry_name = tag.name
        else:
            if tag.template == related_template and tag.column == column_name:
                already_exist = True
                tag_entry_name = tag.name

    # delete if the tag is already existed.
    if already_exist == True:
        print("Tag with given template already existed.")
        request = datacatalog.DeleteTagRequest()
        request.name = tag_entry_name
        result = datacatalog_client.delete_tag(request=request)
        print("Tag Deleted.")

    return True

def get_tag_info(project, dataset, table=""):
    # get all the tag info related with dataset or table
    datacatalog_client = datacatalog.DataCatalogClient()
    request = datacatalog.ListTagsRequest()
    entry = get_entry(project, dataset, table)
    request.parent = entry
    tags = datacatalog_client.list_tags(request=request)
    result = []
    for tag in tags:
        tag_field = str(tag).split('fields ')[1:]
        for field in tag_field:
            tag_info = {"project_id":"", "dataset_name":"", "table_name":"", "column_name":"", 
                        "template_id":"", "template_location":"", "tag_field_id":"", "tag_field_value":""}
            tag_info["project_id"] = project
            tag_info["dataset_name"] = dataset
            tag_info["table_name"] = table
            tag_info["column_name"] = tag.column
            tag_info["template_id"] = tag.template.split("/")[-1]
            tag_info["template_location"] = tag.template.split("/")[3]

            # retrieve filed value manually as ther is no provided way
            key = field.split("}")[0].split(":")[1].split('"')[1]
            value = field.split("}")[0].split(":")[-1].strip().replace('"','')
            tag_info["tag_field_id"] = key
            tag_info["tag_field_value"] = value

            result.append(tag_info)

    return result

def attach_tag(project, template, template_location, tag_info):
    # create a tag for a table
    datacatalog_client = datacatalog.DataCatalogClient()
    tag = datacatalog.Tag()

    # get template definition to define field types
    print(f"Creating tag using template : {template}, location: {template_location}")
    tmpl = get_template(project, template, template_location)

    tag.template = tmpl.name
    
    # prepare dictionary to correct data types
    result_tag_info = prepare_dict(tag_info)
    dataset = result_tag_info["dataset_name"] if "dataset_name" in result_tag_info.keys() and result_tag_info["dataset_name"] != "" else ""
    table = result_tag_info["table_name"] if "table_name" in result_tag_info.keys() and result_tag_info["table_name"] != "" else ""
    
    # flag for every fields not match with template
    no_fields_match = True

    # get fields from template to filter fields which are only availabe in template
    tmpl_field = [field for field in tmpl.fields]

    for key, value in result_tag_info.items():

        if key in tmpl_field:
            no_fields_match = False
            tag.fields[key] = datacatalog.TagField()

            # get the field type from template according to field
            field_type = tmpl.fields[key].type_

            if field_type.primitive_type:
                if str(field_type.primitive_type) == 'PrimitiveType.STRING':
                    tag.fields[key].string_value = value
                if str(field_type.primitive_type) == 'PrimitiveType.DOUBLE':
                    tag.fields[key].double_value = value
                if str(field_type.primitive_type) == 'PrimitiveType.BOOL':
                    tag.fields[key].bool_value = value

            if field_type.enum_type:
                tag.fields[key].enum_value.display_name = value

    if no_fields_match:
        print("Matched fields no found in template. Skipped.")
        return False

    # get table entry and remove tag if existed.
    entry = get_entry(project, dataset, table)
    if entry:
        # check column level tagging or not
        if "column_name" in result_tag_info.keys():
            tag.column = result_tag_info["column_name"]
            # remove column tag if existed
            remove_tag(entry, project, template, template_location, result_tag_info["column_name"])
            try:
                tag = datacatalog_client.create_tag(parent=entry, tag=tag)
                print(f"Attached Tag: {project}.{dataset}.{table} >> {tag.column}")
                result = auto_attach_policy_tag(result_tag_info)    # for policy tagging
                return True
            except Exception:
                print(f"Column Not Found: {project}.{dataset}.{table} >> {tag.column}")
                return False
        else:
            # remove table tag if existed
            remove_tag(entry, project, template, template_location)
            tag = datacatalog_client.create_tag(parent=entry, tag=tag)
            print(f"Attached Tag: {project}.{dataset}.{table}")
            result = auto_attach_policy_tag(result_tag_info)    # for policy tagging
            return True
    else:
        print(f"Not Found: {project}.{dataset}.{table}")
        return False

def read_and_attach_tag():
    job_config = read_json("config/config.json")

    project_id = job_config["project_id"]
    landing_bucket = job_config["tag_landing_bucket"]
    archive_bucket = job_config["tag_archive_bucket"]
    tag_folder = job_config["tag_folder"]
    temp_folder = job_config["temp_folder"]
    default_tmpl_loc = job_config["template_default_location"]

    def attach_tag_info(project_id, tag_info):
        # use default template location if template location is not provided
        if 'template_location' in tag_info.keys() and tag_info['template_location'] != "":
            tmplt_loc = tag_info['template_location']
        else:
            tmplt_loc = default_tmpl_loc

        # use default template if template id is not provided
        if 'template_id' in tag_info.keys() and tag_info['template_id'] != "":
            template = tag_info['template_id']
            # attach tags
            result = attach_tag(project_id, template, tmplt_loc, tag_info)
            return True
        else:
            latest_tmpl_list = get_all_latest_template_id(project_id, "template_", tmplt_loc)
            for tmpl in latest_tmpl_list:
                # attach tags
                result = attach_tag(project_id, tmpl, tmplt_loc, tag_info)
            return True
        return False

    err_rn = ""
    if job_config["run_local"]:

        for tag_file in os.listdir("tags/landing/"):
            if tag_file.endswith(".csv"):
                tag_info_list = read_tag_csv(f"tags/landing/{tag_file}")
                for tag_info in tag_info_list:

                    # call function to tag each row
                    result = attach_tag_info(project_id, tag_info)

                    # write error records to file
                    if result == False:
                        err_rn = tag_file.replace("error_", "")
                        file_exist = os.path.exists(f"tags/error/error_{err_rn}")
                        with open(f"tags/error/error_{err_rn}", 'a') as error_file:
                            writer = csv.DictWriter(error_file, tag_info.keys())
                            if file_exist:
                                writer.writerow(tag_info)
                            else:
                                writer.writeheader()
                                writer.writerow(tag_info)
                    print("-"*50)
                
                os.rename(f"tags/landing/{tag_file}", f"tags/processed/{tag_file}.done")

    else:

        gcs_list = list_file_gcs(project_id, landing_bucket, f"{tag_folder}/")
        for tag_file in gcs_list:
            if tag_file.endswith(".csv"):
                download_file_gcs(project_id, landing_bucket, tag_file, f"{temp_folder}{tag_file.split('/')[-1]}")
                tag_info_list = read_tag_csv(f"{temp_folder}{tag_file.split('/')[-1]}")
                for tag_info in tag_info_list:

                    # call function to tag each row
                    result = attach_tag_info(project_id, tag_info)

                    # write error records to file
                    if result == False:
                        err_rn = tag_file.replace("error_", "").split('/')[-1]
                        file_exist = os.path.exists(f"{temp_folder}error/error_{err_rn}")
                        if not file_exist:
                            os.makedirs(f"{temp_folder}error/")

                        with open(f"{temp_folder}error/error_{err_rn}", 'a') as error_file:
                            writer = csv.DictWriter(error_file, tag_info.keys())
                            if file_exist:
                                writer.writerow(tag_info)
                            else:
                                writer.writeheader()
                                writer.writerow(tag_info)
                    print("-"*50)
                
                # upload error file to gcs
                if os.path.exists(f"{temp_folder}error/error_{err_rn}"):    
                    upload_file_to_gcs(project_id, archive_bucket, f"{temp_folder}error/error_{err_rn}", f"tags/error/error_{err_rn}")
                    os.remove(f"{temp_folder}error/error_{err_rn}")
                    os.removedirs(f"{temp_folder}error/")
                    print("-"*50)
                
                os.remove(f"{temp_folder}{tag_file.split('/')[-1]}")
                move_file_gcs(project_id, landing_bucket, tag_file, archive_bucket, f"{tag_folder}/{tag_file.split('/')[-1]}.done")

    return True