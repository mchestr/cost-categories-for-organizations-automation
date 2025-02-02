"""AWS Cost Categories for Organization automation """
from collections import defaultdict
import json
import hashlib
import datetime
import os
import urllib
import boto3
from botocore.exceptions import ClientError


CATEGORIES_TAGS = list(os.getenv("CCAT_TAGS_LIST").replace(" ","").split(","))
CATEGORIES_START_DATE = datetime.datetime(int(os.getenv("CCAT_START_DATE_YEAR")),
                                          int(os.getenv("CCAT_START_DATE_MONTH")),
                                          1)

SSM_PARAM_ORG_ACC_DIGEST = os.getenv("CCAT_SSM_PATH_ORG_ACC")
SSM_PARAM_ORG_OUS_DIGEST = os.getenv("CCAT_SSM_PATH_ORG_OUS")
DEBUG_FUNC = False

def ce_list_cost_categories(cost_categories_arns):
    """
    List cost categories managed by this script based on owner tag"""

    client = boto3.client('ce')
    ccat_definitions = client.list_cost_category_definitions()
    for ccat in ccat_definitions['CostCategoryReferences']:
        ccat_tags = client.list_tags_for_resource(ResourceArn=ccat['CostCategoryArn'])
        for tag in ccat_tags['ResourceTags']:
            if tag['Key'] == "aws-finops-managed" and tag['Value'] == "true":
                cost_categories_arns[ccat['Name']] = ccat['CostCategoryArn']


def org_list_accounts(org_accounts):
    """
    List all accounts ids in current AWS Organization"""

    client = boto3.client('organizations')
    paginator = client.get_paginator('list_accounts')
    iterator  = paginator.paginate()
    for page in iterator:
        for account in page['Accounts']:
            org_accounts.append(account['Id'])


def recursive_ou_search(parent, org_ous):
    """
    Recursively search OU parent"""

    client = boto3.client('organizations')
    paginator = client.get_paginator('list_organizational_units_for_parent')
    iterator  = paginator.paginate(ParentId=parent)
    for page in iterator:
        for ou_name in page['OrganizationalUnits']:
            org_ous.add(ou_name['Id'])
            recursive_ou_search(ou_name['Id'], org_ous)


def org_list_ous(org_ous):
    """
    List all Organizational Units in the current AWS Organization"""

    org_roots = set()
    client = boto3.client('organizations')

    paginator = client.get_paginator('list_roots')
    iterator  = paginator.paginate()
    for page in iterator:
        for root in page['Roots']:
            org_roots.add(root['Id'])

    recursive_ou_search(org_roots.pop(), org_ous)


def org_fetch_tags_for_account(account, tags_tree):
    """
    List all tags for an AWS account id"""

    client = boto3.client('organizations')
    paginator = client.get_paginator('list_tags_for_resource')
    iterator  = paginator.paginate(ResourceId=account)
    for page in iterator:
        for tags in page['Tags']:
            if tags['Key'] in CATEGORIES_TAGS:
                tags_tree[account].append(tags)


def org_fetch_tags_for_ou(org_unit, tags_tree):
    """
    List all tags for an AWS Organizational Unit id"""

    client = boto3.client('organizations')
    paginator = client.get_paginator('list_tags_for_resource')
    iterator  = paginator.paginate(ResourceId=org_unit)
    for page in iterator:
        for tags in page['Tags']:
            tags_tree[org_unit].append(tags)


def ssm_save_digest(param_path, digest):
    """
    Save AWS Organization objects digest"""

    client = boto3.client('ssm')
    client.put_parameter(
        Name=param_path,
        Description='AWS Organization and Org Units MD5 Digest',
        Value=digest,
        Type='String',
        Overwrite=True,
        Tier='Standard',
        DataType='text'
    )

def ssm_get_digest(param_path):
    """
    Get AWS Organization objects digest"""

    client = boto3.client('ssm')
    try:
        response = client.get_parameter(Name=param_path)
    except ClientError as err:
        if err.response['Error']['Code'] == 'ParameterNotFound':
            return '-1'

    return response['Parameter']['Value']

    
def ce_build_cost_category_definitions(cost_categories_arns, tags_tree):
    """
    Create cost category based on selected tag / values"""

    cc_defs = defaultdict(list)
    cost_categories_map = defaultdict(lambda: defaultdict(list))
    ccat_rules = defaultdict(list)
    ccat_rule = {
        "Value": "", 
        "Rule": {
            "Dimensions": {
                "Key": "LINKED_ACCOUNT",
                "Values": [],
                "MatchOptions": ["EQUALS"]
            }
        },
        "Type": "REGULAR"
    }

    for account, tags_map in tags_tree.items():
        for tag in tags_map:
            cost_categories_map[tag['Key']][tag['Value']].append(account)

    for provided_tag, _ in cost_categories_map.items():
        ccat_rules[provided_tag] = ccat_rule.copy()
        for key in cost_categories_map[provided_tag].items():
            ccat_rules[provided_tag]['Value'] = key[0]
            ccat_rules[provided_tag]['Rule']['Dimensions']['Values'] = key[1]
            cc_defs[provided_tag].append(json.dumps(ccat_rules[provided_tag]))


    client = boto3.client('ce')

    for cc_name, cc_def  in cc_defs.items():
        rules = []
        for rule in cc_def:
            rules.append(json.loads(rule))
        if cc_name in cost_categories_arns:
            print(f"Cost Category \"{cc_name}\" exists and managed by this script. Updating...")
            client.update_cost_category_definition(
                CostCategoryArn=cost_categories_arns[cc_name],
                EffectiveStart=CATEGORIES_START_DATE.strftime("%Y-%m-01T00:00:00Z"),
                RuleVersion='CostCategoryExpression.v1',
                Rules=json.loads(json.dumps(rules)),
            )
        else:
            print(f"Cost Category \"{cc_name}\" detected from tags. Creating...")
            client.create_cost_category_definition(
                Name=cc_name,
                EffectiveStart=CATEGORIES_START_DATE.strftime("%Y-%m-01T00:00:00Z"),
                RuleVersion='CostCategoryExpression.v1',
                Rules=json.loads(json.dumps(rules)),
                ResourceTags=[
                    {
                        'Key': 'aws-finops-managed',
                        'Value': 'true'
                    }]
            )


def hash_dict(obj):
    """
    Helper function to hash dict results"""

    return hashlib.md5(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def send_response(event, context, response):
    """
    Send a response to CloudFormation to handle the custom resource lifecycle"""

    response_body = {
        'Status': response,
        'Reason': 'See details in CloudWatch Log Stream: ' + \
            context.log_stream_name,
        'PhysicalResourceId': context.log_stream_name,
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
    }
    print('RESPONSE BODY: \n' + json.dumps(response_body))
    data = json.dumps(response_body).encode('utf-8')
    req = urllib.request.Request(
        event['ResponseURL'],
        data,
        headers={'Content-Length': len(data), 'Content-Type': ''})
    req.get_method = lambda: 'PUT'
    try:
        with urllib.request.urlopen(req) as resp:
            print(f'response.status: {resp.status}, ' +
                  f'response.reason: {resp.reason}')
            print('response from cfn: ' + resp.read().decode('utf-8'))
    except urllib.error.URLError:
        raise Exception('Received non-200 response while sending response to AWS CloudFormation')
    return True


def lambda_handler(event, context):
    """Lambda function handler"""

    if event.get('RequestType') in ('Create', 'Update'):
        send_response(event, context, "SUCCESS")

    if event.get('RequestType') == 'Delete':
        send_response(event, context, "SUCCESS")
        return # skip function execution

    tags_tree = defaultdict(list)
    cost_categories_arns = {}
    org_accounts = []
    org_ous = set()

    # Debug Lambda function event and context
    if DEBUG_FUNC:
        print(f"{event}\n{context}")

    # Org units
    org_list_ous(org_ous)

    # Org accounts
    org_list_accounts(org_accounts)
    for acc_id in org_accounts:
        org_fetch_tags_for_account(acc_id, tags_tree)

    org_units = list(org_ous)
    org_units.sort()

    if (
        hash_dict(tags_tree) == ssm_get_digest(SSM_PARAM_ORG_ACC_DIGEST) and
        hash_dict(org_units) == ssm_get_digest(SSM_PARAM_ORG_OUS_DIGEST)
        ):
        print("Nothing to do, no change detected on AWS Organizations Accounts and OUs.")
    else:
        ce_list_cost_categories(cost_categories_arns)
        ce_build_cost_category_definitions(cost_categories_arns, tags_tree)

        ssm_save_digest(SSM_PARAM_ORG_ACC_DIGEST, hash_dict(tags_tree))
        ssm_save_digest(SSM_PARAM_ORG_OUS_DIGEST, hash_dict(org_units))



lambda_handler(event="", context="")
